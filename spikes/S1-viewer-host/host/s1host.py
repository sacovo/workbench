"""Spike S1 host: our own transport for ocp-viewer-core, in the project's environment.

One process, one port on 127.0.0.1:
  - plain GET serves the built page (dist/),
  - a websocket with role "page" is our page (the stock viewer mounted through
    ocp-viewer-core's createPage, with our transport and overlay),
  - a websocket with role "control" is the spike driver, which asks for shows,
    face candidates, overlay pins, ray casts and incremental updates.

The show pipeline is ocp-viewer-core's own `Viewer`, driven through a `Comms` we
implement (`PageComms`). Measurement requests from the page go to the core's
`ViewerBackend`, exactly as the stock server does it. Picks forwarded by the page
are resolved here to exact OCCT faces and checked geometrically.

Run: env -u VIRTUAL_ENV uv run python host/s1host.py --port 8765
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import io
import math
import re
import mimetypes
import sys
import threading
import time
import traceback
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import numpy as np
import orjson
from ocp_tessellate.ocp_utils import downcast, get_edges, get_faces, loc_to_tq, serialize, tq_to_loc
from ocp_viewer_core.backend import ViewerBackend
from ocp_viewer_core.codec import default
from ocp_viewer_core.comms import Comms, MessageType, Session
from ocp_viewer_core.config import Camera, Config
from ocp_viewer_core.show import Viewer
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED
from OCP.GeomAbs import GeomAbs_Plane
from OCP.TopLoc import TopLoc_Location
from OCP.BRepTools import BRepTools
from OCP.TopTools import TopTools_FormatVersion
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.gp import gp_Pnt2d
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

import robot
from robot import assembly
from robot.params import PLATFORM
from robot.render import colors_for

SPIKE = Path(__file__).resolve().parents[1]
DIST = SPIKE / "dist"

# The standalone viewer's own resolved defaults (ocp_viewer.server.settings.resolve({})
# with no ~/.ocpvscode_standalone), so the hosted page behaves as the stock one does.
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
# What the page needs before the first model (the stock server fills these into its template).
PAGE_SETTINGS = {k: SETTINGS[k] for k in ("theme", "tree_width", "glass", "tools", "up", "control", "modifier_keys")}

ROOT = "/robot_assembly"


def now_ms() -> float:
    return time.time() * 1000.0


# --------------------------------------------------------------------------------------
# Camera maths, written independently of three.js from the state the page reports
# (position, quaternion, zoom, frustum, canvas rect). Used to check the page's projection.
# --------------------------------------------------------------------------------------


def quat_to_mat(q):
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


class CameraModel:
    """Projection of world points to page pixels, and pixel rays back into the world."""

    def __init__(self, cam: dict):
        self.cam = cam
        self.p = np.array(cam["position"], dtype=float)
        self.R = quat_to_mat(cam["quaternion"])  # camera-to-world rotation
        self.ortho = bool(cam["ortho"])
        rect = cam["canvas"]
        self.rx, self.ry, self.w, self.h = rect["x"], rect["y"], rect["width"], rect["height"]
        if self.ortho:
            zoom = cam["zoom"]
            l, r, t, b = cam["left"], cam["right"], cam["top"], cam["bottom"]
            dx, dy = (r - l) / (2 * zoom), (t - b) / (2 * zoom)
            cx, cy = (r + l) / 2, (t + b) / 2
            self.l, self.r, self.t, self.b = cx - dx, cx + dx, cy + dy, cy - dy
        else:
            self.fov = math.radians(cam["fov"])
            self.aspect = cam["aspect"]

    def to_view(self, P):
        return self.R.T @ (np.asarray(P, dtype=float) - self.p)

    def project(self, P):
        """World point -> (page x, page y, view depth)."""
        v = self.to_view(P)
        if self.ortho:
            nx = (2 * v[0] - (self.r + self.l)) / (self.r - self.l)
            ny = (2 * v[1] - (self.t + self.b)) / (self.t - self.b)
        else:
            f = 1 / math.tan(self.fov / 2)
            nx = (f / self.aspect) * v[0] / -v[2]
            ny = f * v[1] / -v[2]
        return (self.rx + (nx + 1) / 2 * self.w, self.ry + (1 - ny) / 2 * self.h, -v[2])

    def ray(self, px, py):
        """Page pixel -> (origin, unit direction) in world coordinates."""
        nx = (px - self.rx) / self.w * 2 - 1
        ny = 1 - (py - self.ry) / self.h * 2
        if self.ortho:
            xv = (nx * (self.r - self.l) + (self.r + self.l)) / 2
            yv = (ny * (self.t - self.b) + (self.t + self.b)) / 2
            origin = self.p + self.R @ np.array([xv, yv, 0.0])
            d = self.R @ np.array([0.0, 0.0, -1.0])
        else:
            f = 1 / math.tan(self.fov / 2)
            d_view = np.array([nx * self.aspect / f, ny / f, -1.0])
            d = self.R @ (d_view / np.linalg.norm(d_view))
            origin = self.p.copy()
        return origin, d / np.linalg.norm(d)

    def view_dir(self):
        return self.R @ np.array([0.0, 0.0, -1.0])


# --------------------------------------------------------------------------------------
# Exact geometry helpers on the backend's model (ids -> moved OCCT shapes)
# --------------------------------------------------------------------------------------


def pnt(P):
    return gp_Pnt(float(P[0]), float(P[1]), float(P[2]))


def arr(p: gp_Pnt):
    return np.array([p.X(), p.Y(), p.Z()])


def distance_point_shape(P, shape) -> float:
    v = BRepBuilderAPI_MakeVertex(pnt(P)).Vertex()
    d = BRepExtrema_DistShapeShape(v, shape)
    d.Perform()
    return d.Value() if d.IsDone() else float("inf")


def face_normal_at(face, P):
    try:
        surf = BRepAdaptor_Surface(face)
        proj = GeomAPI_ProjectPointOnSurf(pnt(P), BRep_Tool.Surface_s(face))
        if proj.NbPoints() == 0:
            return None
        u, v = proj.LowerDistanceParameters()
        props = BRepLProp_SLProps(surf, u, v, 1, 1e-6)
        if not props.IsNormalDefined():
            return None
    except Exception:  # noqa: BLE001 - projection can fail on degenerate or far points
        return None
    n = props.Normal()
    out = np.array([n.X(), n.Y(), n.Z()])
    if face.Orientation() == TopAbs_REVERSED:
        out = -out
    return out


def face_centroid(face):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return arr(props.CentreOfMass()), props.Mass()


def inner_point(face):
    """A point on the face well inside its boundary: the area centroid when it lies on
    the face, otherwise the UV sample furthest from the boundary."""
    c, area = face_centroid(face)
    if distance_point_shape(c, face) < 1e-4:
        return c, "centroid", area
    surf = BRepAdaptor_Surface(face)
    u0, u1, v0, v1 = surf.FirstUParameter(), surf.LastUParameter(), surf.FirstVParameter(), surf.LastVParameter()
    edges = list(get_edges(face))
    best, best_d = None, -1.0
    n = 9
    for i in range(1, n):
        for j in range(1, n):
            u = u0 + (u1 - u0) * i / n
            v = v0 + (v1 - v0) * j / n
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() != TopAbs_IN:
                continue
            P = arr(surf.Value(u, v))
            d = min(distance_point_shape(P, e) for e in edges) if edges else 0.0
            if d > best_d:
                best, best_d = P, d
    return (best, "inner", area) if best is not None else (None, "none", area)


def facing_inner_point(face, view_dir, n=9, min_dot=0.5):
    """For curved faces: the UV sample inside the face, facing the camera, furthest from the boundary."""
    surf = BRepAdaptor_Surface(face)
    u0, u1, v0, v1 = surf.FirstUParameter(), surf.LastUParameter(), surf.FirstVParameter(), surf.LastVParameter()
    edges = list(get_edges(face))
    best, best_d = None, -1.0
    for i in range(1, n):
        for j in range(1, n):
            u = u0 + (u1 - u0) * i / n
            v = v0 + (v1 - v0) * j / n
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() != TopAbs_IN:
                continue
            props = BRepLProp_SLProps(surf, u, v, 1, 1e-6)
            if not props.IsNormalDefined():
                continue
            nn = props.Normal()
            nv = np.array([nn.X(), nn.Y(), nn.Z()]) * (-1 if face.Orientation() == TopAbs_REVERSED else 1)
            if float(np.dot(nv, -view_dir)) < min_dot:
                continue
            P = arr(surf.Value(u, v))
            d = min(distance_point_shape(P, e) for e in edges) if edges else 0.0
            if d > best_d:
                best, best_d = P, d
    return best


def sample_edges(face, per_edge=48):
    pts = []
    for e in get_edges(face):
        try:
            c = BRepAdaptor_Curve(e)
        except Exception:  # noqa: BLE001
            continue
        a, b = c.FirstParameter(), c.LastParameter()
        for k in range(per_edge + 1):
            pts.append(arr(c.Value(a + (b - a) * k / per_edge)))
    return pts


class RayCaster:
    """Nearest exact intersection of a line with the whole assembly (all faces, world frame)."""

    def __init__(self, faces: dict[str, object]):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound

        builder = BRep_Builder()
        comp = TopoDS_Compound()
        builder.MakeCompound(comp)
        self.order = []
        for fid, f in faces.items():
            builder.Add(comp, f)
            self.order.append(fid)
        self.faces = faces
        self.si = IntCurvesFace_ShapeIntersector()
        t = time.perf_counter()
        self.si.Load(comp, 1e-7)
        self.load_s = time.perf_counter() - t

    def nearest(self, origin, d, tmin=-1e7):
        self.si.Perform(gp_Lin(pnt(origin), gp_Dir(*map(float, d))), tmin, 1e7)
        if not self.si.IsDone() or self.si.NbPnt() == 0:
            return None
        best = None
        for i in range(1, self.si.NbPnt() + 1):
            w = self.si.WParameter(i)
            if best is None or w < best[0]:
                best = (w, i)
        w, i = best
        return {"w": w, "point": arr(self.si.Pnt(i)), "face": self.si.Face(i)}

    def face_id(self, face):
        for fid, f in self.faces.items():
            if f.IsSame(face):
                return fid
        return None


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------


class PageComms(Comms):
    """The transport ocp-viewer-core's show pipeline talks through: our websocket to our page."""

    def __init__(self, host: "Host"):
        super().__init__()
        self.host = host

    def send_data(self, data, timeit=False):
        return self.host.send_model(data)

    def send_config(self, config, timeit=False):
        self.host.to_page(config)

    def send_command(self, data, timeit=False):
        # The two commands the core sends (screenshot, set_relative_time) need no answer.
        self.host.to_page(data)
        return {}

    def status(self):
        return dict(self.host.status)

    def workspace_config(self):
        return {**SETTINGS, "_splash": self.host.splash}

    def send_backend(self, data, timeit=False):
        self.host.load_backend(data)


def _response(status, body, content_type, extra=None):
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", content_type),
            ("Cache-Control", "no-cache"),
        ]
    )
    for k, v in (extra or {}).items():
        headers[k] = v
    return Response(status.value, status.phrase, headers, body)


class Host:
    def __init__(self, dist: Path):
        self.dist = dist.resolve()
        self.page_ws = None
        self.page_ready = threading.Event()
        self.status: dict = {}
        self.splash = True
        self.backend = ViewerBackend()
        self.session = Session(PageComms(self))
        self.config = Config(self.session, ())
        self.viewer = Viewer(self.config)
        self.lock = threading.RLock()
        self.metrics: dict = {}
        self.picks: list = []
        self.expected_next: dict | None = None
        self.page_events: list = []
        self.caster: RayCaster | None = None
        self.parts: dict = {}  # label -> {"loc": tq, "fp": fingerprint}
        self.current_height = None
        self.current_platform = PLATFORM
        self.viewer_locs: dict = {}  # leaf id -> loc as the viewer holds it
        self.log_lines: list = []

    # ---- HTTP ------------------------------------------------------------------------

    def respond(self, connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        path = request.path.partition("?")[0]
        if path == "/":
            path = "/index.html"
        file = (self.dist / path.lstrip("/")).resolve()
        if not file.is_relative_to(self.dist) or not file.is_file():
            return _response(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix in (".js", ".mjs"):
            ctype = "text/javascript; charset=utf-8"
        return _response(HTTPStatus.OK, file.read_bytes(), ctype)

    # ---- websocket -------------------------------------------------------------------

    def handle(self, ws):
        role = None
        try:
            first = orjson.loads(ws.recv())
            role = first.get("role")
            if role == "page":
                self.page_ws = ws
                ws.send(orjson.dumps({"type": "wb_hello", "settings": PAGE_SETTINGS}).decode())
                self.page_ready.set()
                self.log("page connected")
                for raw in ws:
                    self.on_page_message(orjson.loads(raw))
            elif role == "control":
                self.log("control connected")
                for raw in ws:
                    req = orjson.loads(raw)
                    self.on_control(ws, req)
        except Exception:  # noqa: BLE001
            if role is not None:
                self.log(f"{role} handler ended: {traceback.format_exc(limit=3)}")
        finally:
            if role == "page" and self.page_ws is ws:
                self.page_ws = None
                self.page_ready.clear()

    def to_page(self, obj):
        payload = obj if isinstance(obj, str) else orjson.dumps(obj, default=default).decode()
        if self.page_ws is None:
            raise RuntimeError("no page connected")
        self.page_ws.send(payload)
        return len(payload)

    def log(self, msg):
        line = f"[host {time.strftime('%H:%M:%S')}] {msg}"
        self.log_lines.append(line)
        print(line, flush=True)

    # ---- the Comms half --------------------------------------------------------------

    def send_model(self, data):
        m = self.metrics.setdefault("current_show", {})
        m["send_data_called_wall_ms"] = now_ms()
        t = time.perf_counter()
        payload = orjson.dumps(data, default=default)
        m["encode_s"] = time.perf_counter() - t
        t = time.perf_counter()
        text = payload.decode("utf-8")
        m["decode_utf8_s"] = time.perf_counter() - t
        m["payload_bytes"] = len(payload)
        m["send_start_wall_ms"] = now_ms()
        t = time.perf_counter()
        self.page_ws.send(text)
        m["ws_send_s"] = time.perf_counter() - t
        m["send_end_wall_ms"] = now_ms()
        if isinstance(data, dict) and data.get("type") == "data":
            self.splash = False
            # The locations the viewer now holds. ocp-tessellate flattens nested locations, so a
            # leaf's loc is not the build123d shape.location (measured: Pos(c) * Box(...) parts
            # differ by the box's half size). Moves are composed onto these.
            self.viewer_locs = {}

            def walk(node):
                for p in node.get("parts") or []:
                    if p.get("parts") is None:
                        self.viewer_locs[p["id"]] = p.get("loc")
                    else:
                        walk(p)

            walk(data["data"]["shapes"])
        return None

    def load_backend(self, data):
        """As the stock server does on a 'B:' message: encode (BREP as base64), decode, load."""
        m = self.metrics.setdefault("current_show", {})
        t = time.perf_counter()
        payload = orjson.dumps(data, default=default)
        m["backend_encode_s"] = time.perf_counter() - t
        m["backend_payload_bytes"] = len(payload)
        t = time.perf_counter()
        self.backend.handle_event(orjson.loads(payload)["model"], MessageType.DATA)
        m["backend_load_s"] = time.perf_counter() - t
        m["backend_ids"] = len(self.backend.model)

    # ---- page messages ---------------------------------------------------------------

    def on_page_message(self, msg):
        kind = msg.get("kind")
        if kind == "status":
            changes = msg.get("text") or {}
            with self.lock:
                self.status.update(changes)
            # The measurement backend, as the stock server drives it.
            resp = self.backend.handle_event(changes, MessageType.UPDATES)
            if resp is not None:
                self.to_page(resp)
                self.page_events.append({"t": now_ms(), "event": "backend_response", "tool": resp.get("tool_type")})
            if "selectedShapeIDs" in changes:
                self.on_selected(changes["selectedShapeIDs"], msg.get("ctx"))
            if "lastPick" in changes:
                lp = changes["lastPick"]
                self.page_events.append({"t": now_ms(), "event": "lastPick", "path": lp.get("path"), "name": lp.get("name")})
        elif kind == "log":
            self.page_events.append({"t": now_ms(), "event": "log", "text": str(msg.get("text"))[:300]})
        elif kind == "wb_event":
            self.page_events.append({"t": now_ms(), **msg.get("event", {})})

    def on_selected(self, ids, ctx):
        """A measure-tool selection arrived: resolve the id to the exact face and check it."""
        paths = [i for i in ids if isinstance(i, str)]
        if not paths:
            return
        sid = paths[-1]
        rec = {"t": now_ms(), "id": sid, "ids": ids, "ctx": ctx}
        shape = self.backend.model.get(sid)
        rec["resolved"] = shape is not None
        if shape is not None and "/faces/" in sid:
            rec.update(self.check_pick(sid, shape, ctx))
        with self.lock:
            self.picks.append(rec)

    def check_pick(self, sid, face, ctx):
        out = {}
        exp, self.expected_next = self.expected_next, None
        if exp is None:
            return {"expected": None}
        out["expected_id"] = exp["id"]
        out["id_match"] = exp["id"] == sid
        intended = self.backend.model[exp["id"]]
        out["is_same_face"] = bool(intended.IsSame(face))
        P = np.array(exp["point"])
        out["point_to_resolved_face_mm"] = distance_point_shape(P, face)
        if ctx and ctx.get("camera") and ctx.get("pointer"):
            cam = CameraModel(ctx["camera"])
            x, y, _ = cam.project(P)
            out["click_px"] = [ctx["pointer"]["x"], ctx["pointer"]["y"]]
            out["python_projected_px"] = [x, y]
            out["projection_error_px"] = math.hypot(x - ctx["pointer"]["x"], y - ctx["pointer"]["y"])
            # exact anchor where the click ray meets the resolved face
            origin, d = cam.ray(ctx["pointer"]["x"], ctx["pointer"]["y"])
            si = IntCurvesFace_ShapeIntersector()
            si.Load(face, 1e-7)
            si.Perform(gp_Lin(pnt(origin), gp_Dir(*map(float, d))), -1e7, 1e7)
            if si.IsDone() and si.NbPnt() > 0:
                k = min(range(1, si.NbPnt() + 1), key=lambda i: si.WParameter(i))
                A = arr(si.Pnt(k))
                out["anchor"] = A.tolist()
                n = face_normal_at(face, A)
                out["anchor_normal"] = None if n is None else n.tolist()
                out["anchor_to_point_mm"] = float(np.linalg.norm(A - P))
        out["pass"] = bool(out["id_match"] and out["is_same_face"] and out["point_to_resolved_face_mm"] < 1e-3
                           and out.get("projection_error_px", 99) <= 2.0)
        return out

    # ---- control ---------------------------------------------------------------------

    def on_control(self, ws, req):
        rid = req.get("id")
        op = req.get("op")
        try:
            fn = getattr(self, f"op_{op}")
            result = fn(**(req.get("args") or {}))
            ws.send(orjson.dumps({"id": rid, "ok": True, "result": result}, default=_json_default,
                                 option=orjson.OPT_SERIALIZE_NUMPY).decode())
        except Exception as exc:  # noqa: BLE001
            ws.send(orjson.dumps({"id": rid, "ok": False, "error": f"{exc}", "trace": traceback.format_exc()}).decode())

    def op_ping(self):
        return {"robot_file": robot.__file__, "python": sys.version}

    def op_wait_page(self, timeout=20):
        return {"page": self.page_ready.wait(timeout)}

    def op_state(self):
        with self.lock:
            return {"status_keys": sorted(self.status), "splash": self.splash, "metrics": self.metrics,
                    "n_picks": len(self.picks), "backend_ids": len(self.backend.model)}

    def op_events(self, since=0):
        return self.page_events[since:]

    def op_status_get(self, key):
        """A value from the page's status as the host has received it (what the core reads)."""
        with self.lock:
            return self.status.get(key)

    def op_face_area(self, fid):
        return face_centroid(self.backend.model[fid])[1]

    def op_distance(self, a, b):
        d = BRepExtrema_DistShapeShape(self.backend.model[a], self.backend.model[b])
        d.Perform()
        return d.Value()

    # building and showing

    def build(self, height=None, platform=None):
        t = time.perf_counter()
        compound = assembly.robot_assembly(height, platform=platform or self.current_platform)
        colors = colors_for([c.label for c in compound.children])
        for c in compound.children:
            if colors.get(c.label) is not None:
                c.color = colors[c.label]
        return compound, time.perf_counter() - t

    def op_show_robot(self, height=None, reset_camera="reset", platform=None):
        pp = replace(PLATFORM, **platform) if platform else PLATFORM
        compound, build_s = self.build(height, pp)
        self.metrics["current_show"] = {"build_s": build_s, "height": height}
        m = self.metrics["current_show"]
        m["show_start_wall_ms"] = now_ms()
        t = time.perf_counter()
        self.viewer.show(compound, reset_camera=Camera(reset_camera), progress=None)
        m["show_total_s"] = time.perf_counter() - t
        m["show_end_wall_ms"] = now_ms()
        m["tessellate_convert_s"] = (m["send_data_called_wall_ms"] - m["show_start_wall_ms"]) / 1000.0
        self.current_height = height
        self.current_platform = pp
        self.parts = {c.label: {"loc": _tq(c), "fp": fingerprint(c)} for c in compound.children}
        self.caster = None
        m["n_parts"] = len(compound.children)
        m["n_faces_backend"] = sum(1 for k in self.backend.model if "/faces/" in k)
        self.metrics.setdefault("shows", []).append(dict(m))
        return m

    def ensure_caster(self):
        if self.caster is None:
            faces = {k: v for k, v in self.backend.model.items() if "/faces/" in k}
            self.caster = RayCaster(faces)
        return self.caster

    def op_face_candidates(self, camera, parts, per_part=4, edge_margin_px=12.0, ring_px=5.0, max_faces_per_part=400,
                           min_area_mm2=150.0, surface="any"):
        cam = CameraModel(camera)
        caster = self.ensure_caster()
        vd = cam.view_dir()
        out = {}
        stats = {}
        t0 = time.perf_counter()
        for part in parts:
            prefix = f"{ROOT}/{part}/faces/"
            fids = [k for k in self.backend.model if k.startswith(prefix)]
            fids.sort(key=lambda k: int(k.rsplit("_", 1)[1]))
            cands = []
            reasons = {}
            for fid in fids[:max_faces_per_part]:
                face = self.backend.model[fid]
                c, area = face_centroid(face)
                if area < min_area_mm2:
                    reasons["small"] = reasons.get("small", 0) + 1
                    continue
                if surface == "curved":
                    if BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane:
                        reasons["planar"] = reasons.get("planar", 0) + 1
                        continue
                    c = facing_inner_point(face, vd)
                    if c is None:
                        reasons["facing_away"] = reasons.get("facing_away", 0) + 1
                        continue
                n0 = face_normal_at(face, c)
                if n0 is None or float(np.dot(n0, -vd)) < 0.35:
                    reasons["facing_away"] = reasons.get("facing_away", 0) + 1
                    continue
                if surface == "curved":
                    P, how = c, "surface sample"
                else:
                    P, how, area = inner_point(face)
                if P is None:
                    reasons["no_inner_point"] = reasons.get("no_inner_point", 0) + 1
                    continue
                n = face_normal_at(face, P)
                if n is None or float(np.dot(n, -vd)) < 0.35:
                    reasons["facing_away"] = reasons.get("facing_away", 0) + 1
                    continue
                x, y, depth = cam.project(P)
                if not (cam.rx + 30 <= x <= cam.rx + cam.w - 30 and cam.ry + 30 <= y <= cam.ry + cam.h - 30):
                    reasons["off_canvas"] = reasons.get("off_canvas", 0) + 1
                    continue
                # visible: the nearest hit along the pixel's ray is this face at this point
                ok = True
                for (ox, oy) in [(0, 0)] + [(ring_px * math.cos(a), ring_px * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 8, endpoint=False)]:
                    o, d = cam.ray(x + ox, y + oy)
                    hit = caster.nearest(o, d)
                    if hit is None or not hit["face"].IsSame(face):
                        ok = False
                        break
                    if (ox, oy) == (0, 0) and np.linalg.norm(hit["point"] - P) > 0.05:
                        ok = False
                        break
                if not ok:
                    reasons["occluded"] = reasons.get("occluded", 0) + 1
                    continue
                edge_px = min(math.hypot(px - x, py - y) for px, py, _ in (cam.project(q) for q in sample_edges(face)))
                if edge_px < edge_margin_px:
                    reasons["near_edge"] = reasons.get("near_edge", 0) + 1
                    continue
                cands.append({"id": fid, "part": part, "point": P.tolist(), "how": how, "normal": n.tolist(),
                              "surface": str(BRepAdaptor_Surface(face).GetType()).split(".")[-1],
                              "area_mm2": area, "edge_margin_px": edge_px, "python_px": [x, y]})
            cands.sort(key=lambda c: -c["edge_margin_px"])
            out[part] = cands
            stats[part] = {"faces": len(fids), "candidates": len(cands), "rejected": reasons}
        return {"candidates": out, "stats": stats, "elapsed_s": time.perf_counter() - t0, "caster_load_s": caster.load_s}

    def op_expect_pick(self, pick):
        """The face the driver is about to click; the next face selection is checked against it."""
        with self.lock:
            self.expected_next = dict(pick)
        return {"armed": pick["id"]}

    def op_clear_picks(self):
        with self.lock:
            self.picks = []
            self.expected_next = None
        return {"cleared": True}

    def op_picks(self, since=0):
        with self.lock:
            return self.picks[since:]

    def op_project(self, camera, points):
        cam = CameraModel(camera)
        return [list(cam.project(P)) for P in points]

    def op_occlusion(self, camera, points, tol_mm=0.5):
        """For each world point: is something in front of it along the camera ray?"""
        cam = CameraModel(camera)
        caster = self.ensure_caster()
        res = []
        for P in points:
            x, y, depth = cam.project(P)
            o, d = cam.ray(x, y)
            # distance along the ray to P
            tP = float(np.dot(np.asarray(P) - o, d))
            hit = caster.nearest(o, d)
            hidden = hit is not None and hit["w"] < tP - tol_mm
            res.append({"px": [x, y], "hidden": bool(hidden), "t_point": tP,
                        "t_hit": None if hit is None else hit["w"],
                        "hit_face": None if hit is None else caster.face_id(hit["face"])})
        return res

    def op_send_overlay(self, items):
        n = self.to_page({"type": "wb_overlay", "items": items})
        return {"bytes": n}

    def op_to_page(self, message):
        return {"bytes": self.to_page(message)}

    # incremental updates

    def op_diff_update(self, height=None, platform=None, mode="transform"):
        """Rebuild, diff against what is shown, and send only what changed.

        Location-only changes go as new locs (no tessellation); geometry changes are
        re-tessellated per part and sent for updatePart. The backend's exact model is
        updated for the same parts, so picks keep resolving."""
        pp = replace(PLATFORM, **platform) if platform else self.current_platform
        m = {"height": height, "platform": platform, "mode": mode}
        compound, m["build_s"] = self.build(height, pp)
        t = time.perf_counter()
        new = {c.label: {"loc": _tq(c), "fp": fingerprint(c), "shape": c} for c in compound.children}
        moved, regeom = [], []
        for label, info in new.items():
            old = self.parts.get(label)
            if old is None or old["fp"] != info["fp"]:
                regeom.append(label)
            elif not _same_tq(old["loc"], info["loc"]):
                moved.append(label)
        m["diff_s"] = time.perf_counter() - t
        m["moved"] = moved
        m["regeom"] = regeom
        bytes_sent = 0
        # 1. moves: only the new locations travel, as the change of the part's location composed
        #    onto the loc the viewer holds: viewer_new = (L_new * L_old^-1) * viewer_old
        if moved:
            locs = {}
            for lb in moved:
                path = f"{ROOT}/{lb}"
                d_loc = tq_to_loc(*new[lb]["loc"]).Multiplied(tq_to_loc(*self.parts[lb]["loc"]).Inverted())
                t, q = loc_to_tq(d_loc.Multiplied(tq_to_loc(*self.viewer_locs[path])))
                locs[path] = [list(t), list(q)]
            msg = {"type": "wb_set_locs", "mode": mode, "locs": locs, "sent_wall_ms": now_ms()}
            bytes_sent += self.to_page(msg)
            self.viewer_locs.update(locs)
        # 2. geometry: re-tessellate just those parts
        tess = {}
        if regeom:
            t = time.perf_counter()
            for lb in regeom:
                tess[lb] = self.tessellate_one(new[lb]["shape"], lb)
            m["tessellate_s"] = time.perf_counter() - t
            for lb in regeom:
                self.viewer_locs[f"{ROOT}/{lb}"] = tess[lb]["data"]["shapes"]["parts"][0]["loc"]
            msg = {"type": "wb_update_parts", "parts": {f"{ROOT}/{lb}": tess[lb]["data"] for lb in regeom},
                   "sent_wall_ms": now_ms()}
            t = time.perf_counter()
            payload = orjson.dumps(msg, default=default).decode()
            m["encode_s"] = time.perf_counter() - t
            bytes_sent += self.to_page(payload)
        m["bytes_sent"] = bytes_sent
        # 3. keep the exact model in step (moved parts: re-place; regeom: re-load)
        t = time.perf_counter()
        for lb in moved:
            self._backend_move(lb, self.parts[lb]["loc"], new[lb]["loc"])
        for lb in regeom:
            self._backend_reload(lb, tess[lb]["mapping"])
        m["backend_update_s"] = time.perf_counter() - t
        for lb, info in new.items():
            self.parts[lb] = {"loc": info["loc"], "fp": info["fp"]}
        self.current_height = height
        self.current_platform = pp
        self.caster = None
        self.metrics.setdefault("updates", []).append(m)
        return m

    def tessellate_one(self, shape, label):
        """One part through the core's own pipeline, as a one-leaf tree (private: Viewer._convert)."""
        from ocp_tessellate.utils import Color

        colors = [Color(shape.color)] if getattr(shape, "color", None) is not None else None
        self.session.begin({})
        try:
            msg, mapping = self.viewer._convert(shape, names=[label], colors=colors, progress=None)
        finally:
            self.session.clear()
        return {"data": msg["data"], "mapping": mapping}

    def _backend_move(self, label, old_tq, new_tq):
        """Re-place every exact sub-shape of a moved part: x.Moved(L_new * L_old^-1)."""
        prefix = f"{ROOT}/{label}"
        d_loc = tq_to_loc(*new_tq).Multiplied(tq_to_loc(*old_tq).Inverted())
        for k in [k for k in self.backend.model if k == prefix or k.startswith(prefix + "/")]:
            # Moved() returns a plain TopoDS_Shape; face tools need the TopoDS_Face back
            self.backend.model[k] = downcast(self.backend.model[k].Moved(d_loc))

    def _backend_reload(self, label, mapping):
        prefix = f"{ROOT}/{label}"
        for k in [k for k in self.backend.model if k == prefix or k.startswith(prefix + "/")]:
            del self.backend.model[k]
        # the one-leaf mapping has its own root id; re-key it under ours
        payload = orjson.loads(orjson.dumps({"model": mapping}, default=default))
        sub = ViewerBackend()
        sub.handle_event(payload["model"], MessageType.DATA)
        for k, v in sub.model.items():
            tail = k.split(f"/{label}", 1)[1] if f"/{label}" in k else ""
            self.backend.model[prefix + tail] = v

    def op_backend_ids(self, prefix):
        return sorted(k for k in self.backend.model if k.startswith(prefix))

    def op_part_world_bbox(self, labels):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        out = {}
        for lb in labels:
            box = Bnd_Box()
            BRepBndLib.AddOptimal_s(self.backend.model[f"{ROOT}/{lb}"], box, False, False)
            lo, hi = box.CornerMin(), box.CornerMax()
            out[lb] = [lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()]
        return out

    def op_shutdown(self):
        threading.Thread(target=self._stop, daemon=True).start()
        return {"stopping": True}

    def _stop(self):
        time.sleep(0.2)
        self.server.shutdown()


def _tq(shape):
    from ocp_tessellate.ocp_utils import loc_to_tq

    t, q = loc_to_tq(shape.location.wrapped)
    return [list(t), list(q)]


def _same_tq(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a[0] + a[1], b[0] + b[1]))


_FLAGS = re.compile(rb"^[01]{7}$", re.M)


def fingerprint(shape):
    """Geometry identity independent of placement: hash of the unplaced ASCII BREP without
    triangulation and without the TShape flag lines. Tessellating a shape clears its
    'Modified' flag (measured: 0111000 -> 0101000), so a plain BREP hash changes when
    nothing geometric did."""
    unplaced = shape.wrapped.Located(TopLoc_Location())
    buf = io.BytesIO()
    BRepTools.Write_s(unplaced, buf, False, False, TopTools_FormatVersion.TopTools_FormatVersion_VERSION_1)
    return hashlib.sha256(_FLAGS.sub(b"", buf.getvalue())).hexdigest()


def _json_default(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return default(obj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dist", default=str(DIST))
    args = ap.parse_args()
    host = Host(Path(args.dist))
    server = serve(host.handle, "127.0.0.1", args.port, process_request=host.respond,
                   compression=None, max_size=None, ping_interval=None)
    host.server = server
    print(f"READY {args.port} robot={robot.__file__}", flush=True)
    server.serve_forever()
    print("host stopped", flush=True)


if __name__ == "__main__":
    main()
