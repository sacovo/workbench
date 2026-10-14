"""The adapter to ocp-viewer-core and ocp-tessellate; no other kernel module imports them.

The kernel has no page. It runs ocp-viewer-core's own show pipeline (`Session`, `Config`,
`Viewer.show`) over a `Comms` that sends nothing and keeps what the pipeline hands to it. The data
message is therefore exactly what a stock transport receives: tessellated by ocp-tessellate, with
the config renamed to the page's camelCase names and `_core_version` added by
`Session.send_data`. Spike S5 found that a config sent past `Session.send_data` is silently
ignored by the page, which is why nothing here builds that message itself.

The measurement backend is the core's `ViewerBackend`, loaded as the stock server loads it (shapes
encoded as base64 BREP, decoded, `load_model`), with one difference: the stock backend places each
leaf by its own `loc` and ignores the locations of the groups above it, so a root or sub-assembly
with a location of its own would be measured somewhere other than where it is drawn. Here every
leaf's location is composed down the tree before loading, so backend shapes sit where the viewer
draws them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ocp_tessellate.ocp_utils import downcast, loc_to_tq, tq_to_loc
from ocp_viewer_core.backend import ViewerBackend
from ocp_viewer_core.codec import default as codec_default
from ocp_viewer_core.comms import Comms, MessageType, Session
from ocp_viewer_core.config import Config
from ocp_viewer_core.show import Viewer
from OCP.TopLoc import TopLoc_Location

# The stock standalone viewer's resolved defaults (ocp_viewer.server.settings.resolve({}) with no
# ~/.ocpvscode_standalone), as spike S1 used them: the hosted page behaves as the stock one does,
# and `deviation` and `angular_tolerance` set the tessellation the page receives.
SETTINGS = {
    "ambient_intensity": 1, "angular_tolerance": 0.2, "axes": False, "axes0": True,
    "black_edges": False, "center_grid": False, "collapse": "leaves", "control": "trackball",
    "debug": False, "default_color": "#e8b024", "default_edgecolor": "#707070",
    "default_facecolor": "Violet", "default_opacity": 0.5, "default_thickedgecolor": "MediumOrchid",
    "default_vertexcolor": "MediumOrchid", "deviation": 0.1, "direct_intensity": 1.1,
    "explode": False, "glass": True, "grid": [False, False, False], "grid_font_size": 12,
    "metalness": 0.3,
    "modifier_keys": {"shift": "shiftKey", "ctrl": "ctrlKey", "meta": "altKey", "alt": "metaKey"},
    "new_tree_behavior": True, "ortho": True, "pan_speed": 1, "python_enumeration": True,
    "reset_camera": "KEEP", "rotate_speed": 1, "roughness": 0.65, "theme": "light", "ticks": 5,
    "tools": True, "transparent": False, "tree_width": 240, "up": "Z", "zoom_speed": 1,
}

SUB_SHAPE_DIRS = ("faces", "edges", "vertices")


class _CaptureComms(Comms):
    """A transport that sends nothing: the show pipeline's model and backend mapping are kept.

    `status` and `workspace_config` answer as a page would: `splash` says whether the page still
    shows the logo (then the pipeline resets the camera and ignores the page's state), and
    `viewer_status` is the page's last status snapshot (its toolbar and tree state)."""

    def __init__(self):
        super().__init__()
        self.splash = True
        self.viewer_status: dict = {}
        self.data = None
        self.backend = None

    def send_data(self, data, timeit=False):
        self.data = data

    def send_config(self, config, timeit=False):
        raise RuntimeError("the kernel sends no viewer config; the hub talks to the page")

    def send_command(self, data, timeit=False):
        raise RuntimeError("the kernel sends no viewer commands; the hub talks to the page")

    def status(self):
        return dict(self.viewer_status)

    def workspace_config(self):
        return {**SETTINGS, "_splash": self.splash}

    def send_backend(self, data, timeit=False):
        self.backend = data


@dataclass
class Shown:
    payload: dict  # the data message as Session.send_data hands it to a transport
    tree: dict  # payload["data"]["shapes"]: the viewer's tree with each node's id and loc
    model: dict  # backend id -> OCCT shape placed where the viewer draws it


def loc_tq(loc: TopLoc_Location) -> list:
    """A location as the viewer writes it: [[x, y, z], [qx, qy, qz, qw]]."""
    t, q = loc_to_tq(loc)
    return [list(t), list(q)]


def tq_loc(tq) -> TopLoc_Location:
    return TopLoc_Location() if tq is None else tq_to_loc(*tq)


def is_sub_shape(shape_id: str, path: str) -> bool:
    """Whether `shape_id` under `path` names a face, edge or vertex rather than a leaf shape."""
    return any(f"/{d}/" in shape_id[len(path):] for d in SUB_SHAPE_DIRS)


def world_locs(tree: dict, parent: TopLoc_Location | None = None) -> dict[str, TopLoc_Location]:
    """Leaf id -> the leaf's location composed with every group above it (the drawn placement)."""
    out: dict[str, TopLoc_Location] = {}

    def walk(node, above):
        here = above if node.get("loc") is None else above.Multiplied(tq_loc(node["loc"]))
        if node.get("parts") is None:
            out[node["id"]] = here
        else:
            for child in node["parts"]:
                walk(child, here)

    walk(tree, TopLoc_Location() if parent is None else parent)
    return out


def _with_locs(mapping: dict, locs: dict[str, TopLoc_Location]) -> dict:
    """A copy of a backend mapping with each leaf's loc replaced by its composed location."""
    out = {k: v for k, v in mapping.items() if k != "parts"}
    out["parts"] = [
        _with_locs(p, locs) if p.get("parts") is not None else {**p, "loc": locs[p["id"]]} for p in mapping["parts"]
    ]
    return out


def backend_model(mapping: dict, locs: dict[str, TopLoc_Location]) -> dict:
    """The backend's id -> shape table for a mapping, loaded as the stock server loads it.

    `load_model` is called directly: `handle_event` swallows exceptions and would leave a partial
    table without a word. Every leaf of the tree must come back, or this raises."""
    wire = json.loads(json.dumps({"model": _with_locs(mapping, locs)}, default=codec_default))
    backend = ViewerBackend()
    backend.load_model(wire["model"])
    missing = [leaf for leaf in locs if leaf not in backend.model]
    if missing:
        raise RuntimeError(f"the measurement backend holds no shape for {len(missing)} leaves, e.g. {missing[:3]}")
    return backend.model


def moved_entries(model: dict, ids: list[str], delta: TopLoc_Location) -> dict:
    """The backend shapes `ids`, moved by `delta`. `Moved` returns a plain TopoDS_Shape and face
    functions fail quietly on it (spike S1, fix 2), so each one is downcast again."""
    return {k: downcast(model[k].Moved(delta)) for k in ids}


class ViewerHost:
    """ocp-viewer-core's show pipeline, run for its output."""

    def __init__(self):
        self._comms = _CaptureComms()
        self._viewer = Viewer(Config(Session(self._comms), ()))
        # Single parts go through a viewer of their own: a show updates its viewer's memory of the
        # last model (bounding box, paths), which decides what the next full show keeps.
        self._part_comms = _CaptureComms()
        self._part_viewer = Viewer(Config(Session(self._part_comms), ()))
        self._backend = ViewerBackend()

    @staticmethod
    def _run(viewer, comms, obj, splash, status, **kwargs) -> tuple[dict, dict]:
        comms.splash = splash
        comms.viewer_status = dict(status or {})
        comms.data = comms.backend = None
        viewer.show(obj, progress="", **kwargs)
        if comms.data is None or comms.backend is None:
            raise RuntimeError("ocp-viewer-core's show sent no model; is the object empty?")
        return comms.data, comms.backend["model"]

    def show(self, compound, splash: bool = True, status: dict | None = None) -> Shown:
        """The data message for `compound` and its backend table."""
        data, mapping = self._run(self._viewer, self._comms, compound, splash, status)
        tree = data["data"]["shapes"]
        return Shown(data, tree, backend_model(mapping, world_locs(tree)))

    def show_part(self, shape, name: str, path: str, root_location: TopLoc_Location) -> tuple[dict, list, dict]:
        """One part on its own, as the page's updatePart takes it: (one-leaf tessellation, the
        leaf's loc relative to the assembly root, backend shapes re-keyed under `path`)."""
        data, mapping = self._run(self._part_viewer, self._part_comms, shape, True, None, names=[name])
        tree = data["data"]["shapes"]
        leaves = [p for p in tree.get("parts") or [] if p.get("parts") is None]
        if len(tree.get("parts") or []) != 1 or len(leaves) != 1:
            raise RuntimeError(f"part {name!r} does not tessellate to one leaf")
        leaf = leaves[0]
        model = backend_model(mapping, world_locs(tree, parent=root_location))
        rekeyed = {path + k[len(leaf["id"]):]: v for k, v in model.items() if k == leaf["id"] or k.startswith(leaf["id"] + "/")}
        return data["data"], leaf["loc"], rekeyed

    def backend_reply(self, model: dict, changes: dict):
        """The stock backend's answer to a status change set from the page (None: nothing to say)."""
        self._backend.model = model
        return self._backend.handle_event(changes, MessageType.UPDATES)


def encode_payload(payload: dict) -> bytes:
    """A data message as JSON bytes, shapes and locations written by the core's own codec. NaN is
    refused: the stock encoder (orjson) would write null, the page's JSON.parse rejects NaN."""
    return json.dumps(payload, default=codec_default, separators=(",", ":"), allow_nan=False).encode()


def json_default(obj):
    """Encoder fallback for protocol results: numpy values, then the core codec (shapes as base64
    BREP, locations as [t, q], enums by value)."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return codec_default(obj)
