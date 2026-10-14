"""A stand-in for workbench_kernel in the hub tests: the same JSON-lines protocol, no CAD.

    python tests/fake_kernel.py serve --project DIR

It answers hello, build, update, resolve and backend the way the real kernel shapes them
(workbench_kernel.ops), writes its data messages to DIR/.workbench/blobs/<sha>.json like the real
one, and puts its own pid in every result so a test can tell which kernel answered. Scene ids
are "<token>-<n>" per process, so two kernels never share one.

Behaviour is read from DIR/.workbench/fake-kernel.json before each request, so a test can change
what the next request does: {"build_delay": s, "crash_on": "build" | "hello", "fail_on": "build"}.

The assembly "demo" has three parts; its argument `height` moves the part "carriage", and its
argument `width` re-shapes the part "column" (a changed part).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ASSEMBLIES = {
    "demo": {"doc": "three parts", "builder": "demo.manifest.demo", "args": [
        {"name": "height", "default": 100.0, "annotation": "float"},
        {"name": "width", "default": 10.0, "annotation": "float"},
    ]},
    "other": {"doc": "one part", "builder": "demo.manifest.other", "args": []},
}
PARTS = {"demo": ["base", "column", "carriage"], "other": ["lonely"]}


class RequestError(Exception):
    pass


def main() -> int:
    root = Path(sys.argv[sys.argv.index("--project") + 1]).resolve()
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # anything printed lands on stderr, as in the real kernel
    token = os.urandom(3).hex()
    state = {"count": 0, "scenes": {}, "current": {}, "latest": None, "tool": None}

    def control() -> dict:
        try:
            return json.loads((root / ".workbench" / "fake-kernel.json").read_text())
        except (OSError, ValueError):
            return {}

    def write_blob(payload: dict, kind: str) -> dict:
        data = json.dumps(payload, separators=(",", ":")).encode()
        sha = hashlib.sha256(data).hexdigest()
        directory = root / ".workbench" / "blobs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{sha}.json").write_bytes(data)
        return {"kind": kind, "sha": sha, "path": str(directory / f"{sha}.json"), "bytes": len(data)}

    def resolve_args(assembly: str, args: dict | None) -> dict:
        if assembly not in ASSEMBLIES:
            raise RequestError(f"no assembly {assembly!r}; the manifest has {sorted(ASSEMBLIES)}")
        known = {a["name"]: a["default"] for a in ASSEMBLIES[assembly]["args"]}
        unknown = sorted(set(args or {}) - set(known))
        if unknown:
            raise TypeError(f"assembly {assembly!r} takes no argument {unknown}")
        return {**known, **(args or {})}

    def new_scene(assembly: str, args: dict) -> dict:
        state["count"] += 1
        sid = f"{token}-{state['count']}"
        root_path = f"/{assembly}_assembly"
        parts = [{"path": f"{root_path}/{label}", "label": label, "fingerprint": f"{label}-{args.get('width')}",
                  "n_faces": 6, "bbox": [0, 0, 0, 1, 1, 1]} for label in PARTS[assembly]]
        scene = {"scene": sid, "assembly": assembly, "args": args, "root": root_path, "parts": parts}
        state["scenes"][sid] = scene
        state["current"][assembly] = sid
        state["latest"] = sid
        return scene

    def loc(label: str, args: dict) -> list:
        z = args.get("height", 0.0) if label == "carriage" else 0.0
        return [[0.0, 0.0, z], [0.0, 0.0, 0.0, 1.0]]

    def op_hello() -> dict:
        if control().get("crash_on") == "hello":
            os._exit(3)
        return {"pid": os.getpid(), "kernel_version": "fake", "protocol": 1, "project_root": str(root),
                "manifest_module": "demo.manifest", "manifest": {"name": "demo", "units": "mm", "assemblies": ASSEMBLIES, "params": {}},
                "manifest_error": None}

    def op_build(assembly: str, args: dict | None = None, viewer: dict | None = None) -> dict:
        c = control()
        resolved = resolve_args(assembly, args)
        time.sleep(float(c.get("build_delay", 0)))
        if c.get("crash_on") == "build":
            os._exit(3)
        if c.get("fail_on") == "build":
            raise ValueError("deliberate failure in a builder")
        scene = new_scene(assembly, resolved)
        splash = True if viewer is None else bool(viewer.get("splash", True))
        payload = {"type": "data", "count": len(scene["parts"]),
                   "config": {"resetCamera": "reset" if splash else "keep", "_core_version": "1.1.2"},
                   "data": {"instances": [], "shapes": {"id": scene["root"], "name": scene["root"][1:], "loc": None,
                            "parts": [{"id": p["path"], "name": p["label"], "loc": loc(p["label"], resolved)} for p in scene["parts"]]}}}
        return {**scene, "blob": write_blob(payload, "show"), "timings": {"total_s": 0.0}, "kernel_pid": os.getpid()}

    def op_update(assembly: str, args: dict | None = None, viewer: dict | None = None) -> dict:
        resolved = resolve_args(assembly, args)
        previous = state["scenes"].get(state["current"].get(assembly))
        if previous is None:
            return {**op_build(assembly, args, viewer), "previous": None, "incremental": False,
                    "reason": "no earlier scene of this assembly in this kernel"}
        time.sleep(float(control().get("build_delay", 0)))
        old = previous["args"]
        scene = new_scene(assembly, resolved)
        moved = [{"path": f"{scene['root']}/carriage", "label": "carriage", "loc": loc("carriage", resolved)}] if resolved.get("height") != old.get("height") else []
        changed = [{"path": f"{scene['root']}/column", "label": "column"}] if resolved.get("width") != old.get("width") else []
        blob = write_blob({"parts": {c["path"]: {"instances": [], "shapes": {"parts": [{"id": "/column", "loc": None}]}} for c in changed}}, "parts") if changed else None
        return {**scene, "previous": previous["scene"], "incremental": True, "reason": None, "moved": moved,
                "changed": changed, "blob": blob, "timings": {"total_s": 0.0}, "kernel_pid": os.getpid()}

    def op_resolve(scene: str, ids: list) -> dict:
        sc = state["scenes"].get(scene)
        if sc is None:
            raise RequestError(f"no scene {scene!r} in this kernel")
        results = []
        for shape_id in ids:
            rest = shape_id[len(sc["root"]) + 1:] if isinstance(shape_id, str) and shape_id.startswith(sc["root"] + "/") else None
            label = rest.split("/")[0] if rest else None
            if label not in PARTS[sc["assembly"]]:
                results.append({"id": shape_id, "found": False, "error": f"scene {scene} has no shape {shape_id!r}"})
                continue
            kind = "face" if "/faces/" in rest else "edge" if "/edges/" in rest else "vertex" if "/vertices/" in rest else "solid"
            world = {"vertex": {"geom_type": "point", "point": [1.0, 2.0, 3.0], "centre": [1.0, 2.0, 3.0]},
                     "face": {"geom_type": "plane", "area": 2.5, "centre": [0.5, 0.5, 1.0], "normal": [0.0, 0.0, 1.0]},
                     "edge": {"geom_type": "line", "length": 1.0, "centre": [0.5, 0.0, 1.0]},
                     "solid": {"volume": 1.0, "area": 6.0, "centre": [0.5, 0.5, 0.5], "bbox": [0, 0, 0, 1, 1, 1]}}[kind]
            results.append({"id": shape_id, "found": True, "part": {"label": label, "path": f"{sc['root']}/{label}"},
                            "kind": kind, "world": world, "local": world, "kernel_pid": os.getpid()})
        return {"scene": scene, "results": results, "found": sum(r["found"] for r in results), "kernel_pid": os.getpid()}

    def op_backend(message: dict, scene: str | None = None) -> dict:
        if "activeTool" in message:
            state["tool"] = None if message["activeTool"] == "None" else message["activeTool"]
        reply = None
        if state["tool"] and message.get("selectedShapeIDs"):
            reply = {"type": "backend_response", "tool_type": state["tool"], "subtype": "fake",
                     "result": {"ids": message["selectedShapeIDs"], "scene": scene, "kernel_pid": os.getpid()}}
        return {"scene": scene or state["latest"], "reply": reply, "unknown_ids": []}

    ops = {"hello": op_hello, "build": op_build, "update": op_update, "resolve": op_resolve, "backend": op_backend}
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        rid = None
        try:
            request = json.loads(line)
            rid = request.get("id")
            result = ops[request["op"]](**(request.get("args") or {}))
            response = {"id": rid, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - every failure is an answer, as in the real kernel
            response = {"id": rid, "ok": False, "error": {"type": type(exc).__name__, "message": str(exc), "traceback": f"Traceback (most recent call last):\n  File \"{__file__}\", line 1, in fake\n{type(exc).__name__}: {exc}\n"}}
        out.write(json.dumps(response).encode() + b"\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
