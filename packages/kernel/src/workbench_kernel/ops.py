"""The operations the kernel answers, over one project's manifest and the scenes built from it.

A scene is one built assembly as the viewer would show it: an id, its parts, and the backend
table that resolves pick ids. The kernel keeps the current and the previous scene of each
assembly, so a pick made on the scene the page still shows resolves while an update replaces it.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
import secrets
import sys
import time
import traceback
from pathlib import Path

from workbench_kernel import __version__, host, paths, resolve
from workbench_kernel import scene as scenes
from workbench_kernel.api import ManifestError, Project
from workbench_kernel.facts import part_facts

PROTOCOL = 1
SCENES_KEPT_PER_ASSEMBLY = 2
_VERSIONED = ("workbench-kernel", "build123d", "cadquery-ocp-novtk", "cadquery-ocp", "ocp-tessellate", "ocp-viewer-core")


class RequestError(Exception):
    """The request cannot be served as asked (unknown scene, bad arguments)."""


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _timed(timings: dict, key: str, fn, *args, **kwargs):
    t = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        timings[key] = round(time.perf_counter() - t, 4)


class Kernel:
    def __init__(self, project_root: Path, imports_s: float | None = None):
        self.project_root = project_root.resolve()
        self.project: Project | None = None
        self.manifest_module: str | None = None
        self.manifest_file: str | None = None
        self.code_root: str | None = None
        self.manifest_error: BaseException | None = None
        self.startup = {"kernel_imports_s": None if imports_s is None else round(imports_s, 4)}
        _timed(self.startup, "manifest_import_s", self._load_manifest)
        self.host = host.ViewerHost()
        self.scenes: dict[str, scenes.Scene] = {}
        self.history: dict[str, list[str]] = {}
        self.latest: str | None = None
        self._token = secrets.token_hex(3)
        self._count = 0

    # ---- manifest ----------------------------------------------------------------------

    def _load_manifest(self) -> None:
        """Import the manifest once. A failure is kept and reported by every op except hello."""
        try:
            self.manifest_module = paths.manifest_module(self.project_root)
            module = importlib.import_module(self.manifest_module)
            self.manifest_file = getattr(module, "__file__", None)
            top = sys.modules[self.manifest_module.split(".")[0]]
            top_file = getattr(top, "__file__", None)
            self.code_root = str(Path(top_file).resolve().parent) if top_file else str(Path(list(top.__path__)[0]).resolve())
            projects = [v for v in vars(module).values() if isinstance(v, Project)]
            if len(projects) != 1:
                raise ManifestError(f"{self.manifest_module} must create exactly one workbench Project, found {len(projects)}")
            projects[0].check_params()
            self.project = projects[0]
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - reported to the hub, never swallowed
            self.manifest_error = exc

    def _project(self) -> Project:
        if self.project is None:
            raise ManifestError(f"the manifest could not be loaded: {self.manifest_error!r}") from self.manifest_error
        return self.project

    def _spec(self, assembly: str):
        project = self._project()
        if assembly not in project.assemblies:
            raise RequestError(f"no assembly {assembly!r}; the manifest has {sorted(project.assemblies)}")
        return project.assemblies[assembly]

    # ---- scenes ------------------------------------------------------------------------

    def _scene(self, scene_id: str) -> scenes.Scene:
        if scene_id not in self.scenes:
            current = {a: ids[-1] for a, ids in self.history.items()}
            raise RequestError(f"no scene {scene_id!r} in this kernel; current scenes: {current}")
        return self.scenes[scene_id]

    def _current(self, assembly: str) -> scenes.Scene | None:
        ids = self.history.get(assembly)
        return self.scenes[ids[-1]] if ids else None

    def _keep(self, sc: scenes.Scene) -> None:
        self.scenes[sc.id] = sc
        ids = self.history.setdefault(sc.assembly, [])
        ids.append(sc.id)
        while len(ids) > SCENES_KEPT_PER_ASSEMBLY:
            del self.scenes[ids.pop(0)]
        self.latest = sc.id

    def _new_id(self) -> str:
        self._count += 1
        return f"{self._token}-{self._count}"

    @staticmethod
    def _viewer_state(viewer) -> tuple[bool, dict]:
        """(splash, status) of the page the data message is meant for. With no `viewer`, a page
        that still shows the logo: the camera is reset to fit the model."""
        viewer = {} if viewer is None else viewer
        if not isinstance(viewer, dict) or set(viewer) - {"splash", "status"}:
            raise RequestError('viewer must be {"splash": bool, "status": {...}}')
        status = viewer.get("status") or {}
        if not isinstance(status, dict):
            raise RequestError("viewer.status must be an object")
        return bool(viewer.get("splash", True)), status

    def _call_builder(self, spec, args: dict, timings: dict):
        compound = _timed(timings, "build_s", spec.builder, **args)
        scenes.children_of(compound, spec.name)
        return compound

    def _show(self, spec, args, compound, drafts, viewer, timings) -> scenes.Scene:
        splash, status = self._viewer_state(viewer)
        shown = _timed(timings, "tessellate_s", self.host.show, compound, splash, status)
        parts = _timed(timings, "parts_s", scenes.attach, drafts, shown.tree, shown.model)
        payload = _timed(timings, "encode_s", host.encode_payload, shown.payload)
        blob = _timed(timings, "blob_write_s", paths.write_blob, self.project_root, payload, "show")
        sc = scenes.Scene(self._new_id(), spec.name, args, shown.tree["id"], compound.location.wrapped, compound, parts, shown.model, blob)
        self._keep(sc)
        return sc

    @staticmethod
    def _describe(sc: scenes.Scene) -> dict:
        return {"scene": sc.id, "assembly": sc.assembly, "args": sc.args, "root": sc.root_path, "parts": [p.describe() for p in sc.parts.values()]}

    # ---- operations ----------------------------------------------------------------------

    def op_hello(self) -> dict:
        error = None
        if self.manifest_error is not None:
            error = {
                "type": type(self.manifest_error).__name__,
                "message": str(self.manifest_error),
                "traceback": "".join(traceback.format_exception(self.manifest_error)),
            }
        code_root_in_project = None
        if self.code_root is not None:
            code_root_in_project = Path(self.code_root).is_relative_to(self.project_root)
        return {
            "pid": os.getpid(),
            "kernel_version": __version__,
            "protocol": PROTOCOL,
            "python": platform.python_version(),
            "executable": sys.executable,
            "project_root": str(self.project_root),
            "manifest_module": self.manifest_module,
            "manifest_file": self.manifest_file,
            "code_root": self.code_root,
            "code_root_in_project": code_root_in_project,
            "manifest": None if self.project is None else self.project.describe(),
            "manifest_error": error,
            "versions": {d: v for d in _VERSIONED if (v := _version(d)) is not None},
            "startup": self.startup,
        }

    def op_build(self, assembly: str, args: dict | None = None, viewer: dict | None = None) -> dict:
        """Build, tessellate and store the data message for `assembly` with `args`."""
        spec = self._spec(assembly)
        resolved = spec.resolve_args(args)
        timings: dict = {}
        t = time.perf_counter()
        compound = self._call_builder(spec, resolved, timings)
        drafts = _timed(timings, "fingerprint_s", scenes.drafts, compound, spec.name)
        sc = self._show(spec, resolved, compound, drafts, viewer, timings)
        timings["total_s"] = round(time.perf_counter() - t, 4)
        return {**self._describe(sc), "blob": sc.blob, "timings": timings}

    def op_update(self, assembly: str, args: dict | None = None, viewer: dict | None = None) -> dict:
        """Rebuild and compare with the current scene of `assembly`: moved parts come back as new
        viewer locs, changed parts re-tessellated in a blob. Anything else is a full build, and
        the result says why."""
        spec = self._spec(assembly)
        resolved = spec.resolve_args(args)
        timings: dict = {}
        t = time.perf_counter()
        previous = self._current(assembly)
        compound = self._call_builder(spec, resolved, timings)
        drafts = _timed(timings, "fingerprint_s", scenes.drafts, compound, spec.name)
        plan = "no earlier scene of this assembly in this kernel" if previous is None else scenes.plan_update(previous, compound, drafts)
        if isinstance(plan, str):
            sc = self._show(spec, resolved, compound, drafts, viewer, timings)
            timings["total_s"] = round(time.perf_counter() - t, 4)
            return {
                **self._describe(sc), "previous": None if previous is None else previous.id,
                "incremental": False, "reason": plan, "blob": sc.blob, "timings": timings,
            }
        moved, changed = plan
        t_apply = time.perf_counter()
        model = dict(previous.model)
        groups = scenes.ids_by_part(previous.model, previous.root_path)
        parts: dict[str, scenes.Part] = {}
        moved_out, changed_out, tessellated = [], [], {}
        for d in drafts:
            old = previous.parts[d.label]
            ids = groups.get(d.label, [])
            part = scenes.Part(d.label, old.path, d.shape, d.location, d.placement, d.fingerprint, d.look,
                              old.viewer_loc, old.leaf, old.n_faces, old.bbox)
            if d.label in moved:
                # Backend shapes move by the change of placement in the world; the viewer's loc,
                # relative to the root group, by the change of the child's own location.
                model.update(host.moved_entries(previous.model, ids, d.placement.Multiplied(old.placement.Inverted())))
                delta = d.location.Multiplied(old.location.Inverted())
                part.viewer_loc = host.loc_tq(delta.Multiplied(host.tq_loc(old.viewer_loc)))
                part.bbox = scenes.bbox_of(scenes.leaf_shapes(model, ids, old.path))
                moved_out.append({"path": old.path, "label": d.label, "loc": part.viewer_loc})
            elif d.label in changed:
                data, leaf_loc, entries = self.host.show_part(d.shape, d.label, old.path, previous.root_location)
                for k in ids:
                    del model[k]
                model.update(entries)
                tessellated[old.path] = data
                part.viewer_loc = leaf_loc
                part.n_faces = scenes.count_faces(list(entries), old.path)
                part.bbox = scenes.bbox_of(scenes.leaf_shapes(model, list(entries), old.path))
                changed_out.append({"path": old.path, "label": d.label})
            parts[d.label] = part
        timings["apply_s"] = round(time.perf_counter() - t_apply, 4)
        blob = None
        if tessellated:
            payload = _timed(timings, "encode_s", host.encode_payload, {"parts": tessellated})
            blob = _timed(timings, "blob_write_s", paths.write_blob, self.project_root, payload, "parts")
        sc = scenes.Scene(self._new_id(), spec.name, resolved, previous.root_path, previous.root_location, compound, parts, model, blob)
        self._keep(sc)
        timings["total_s"] = round(time.perf_counter() - t, 4)
        return {
            **self._describe(sc), "previous": previous.id, "incremental": True, "reason": None,
            "moved": moved_out, "changed": changed_out, "blob": blob, "timings": timings,
        }

    def op_resolve(self, scene: str, ids: list) -> dict:
        """Pick ids of a scene to parts and exact properties, in world and part-local coordinates."""
        sc = self._scene(scene)
        if not isinstance(ids, list) or not ids:
            raise RequestError("ids must be a non-empty list of viewer pick ids")
        results = [resolve.resolve(sc, i) for i in ids]
        return {"scene": sc.id, "results": results, "found": sum(r["found"] for r in results)}

    def op_backend(self, message: dict, scene: str | None = None) -> dict:
        """A measure-tool status change set from the stock page, answered by the stock backend over
        the scene's shapes (the latest scene unless named). `reply` is what the page waits for."""
        if not isinstance(message, dict):
            raise RequestError("message must be the status change set the page sent")
        if scene is None and self.latest is None:
            raise RequestError("no scene has been built yet")
        sc = self._scene(scene if scene is not None else self.latest)
        selected = message.get("selectedShapeIDs") or []
        unknown = [i for i in selected if isinstance(i, str) and i not in sc.model]
        reply = self.host.backend_reply(sc.model, message)
        return {"scene": sc.id, "reply": reply, "unknown_ids": unknown}

    def op_facts(self, assembly: str, args: dict | None = None) -> dict:
        """Volume, exact bounding box and BRep validity per part. Measured on the current scene's
        parts when it was built with the same arguments, otherwise on a fresh build (not shown)."""
        spec = self._spec(assembly)
        resolved = spec.resolve_args(args)
        timings: dict = {}
        t = time.perf_counter()
        current = self._current(assembly)
        if current is not None and current.args == resolved:
            compound, source = current.compound, current.id
        else:
            compound, source = self._call_builder(spec, resolved, timings), None
        drafts = _timed(timings, "fingerprint_s", scenes.drafts, compound, spec.name)
        facts = _timed(timings, "facts_s", lambda: [part_facts(d) for d in drafts])
        timings["total_s"] = round(time.perf_counter() - t, 4)
        return {"assembly": spec.name, "args": resolved, "scene": source, "parts": facts, "timings": timings}


def encode(obj) -> bytes:
    """One protocol line's JSON. NaN and infinity are refused rather than written as tokens the
    page's JSON.parse would reject."""
    return json.dumps(obj, default=host.json_default, separators=(",", ":"), allow_nan=False).encode()
