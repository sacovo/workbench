"""A compiled MuJoCo model as a three-cad-viewer scene: one group per body, one leaf per geom, in mm.

Body groups sit flat under the scene root and carry the body's world pose, so a body's group
transform in the page is directly comparable with `data.xpos` / `data.xquat`. Each geom is a leaf
placed by `geom_pos` / `geom_quat` in its body group.

Primitives are either build123d solids tessellated by ocp-tessellate (`primitives="brep"`: faces
and edges like any CAD part) or tessellated here with numpy (`primitives="direct"`). Mesh geoms
are always raw leaves built from `mesh_vert` / `mesh_face` / `mesh_normal`, with no B-rep. Shared
geometry (the same mesh asset, identical left and right arm meshes, equal primitives) is sent
once in `instances` and referenced from each leaf by `{"ref": n}`, as ocp-tessellate does.

Viewer quaternions are (x, y, z, w); MuJoCo's are (w, x, y, z).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

import mujoco
import numpy as np

MM = 1000.0
DEFAULT_RGBA = np.array([0.5, 0.5, 0.5, 1.0])
VISUAL_GROUPS = (0, 1, 2)  # what MuJoCo's own viewer shows by default
PLANE_HALF_MM = 600.0  # a MuJoCo plane is drawn this big (its own size is 5 m) so the camera frames the robot
_RESERVED = re.compile(r"[\[\]\.:/|\s]")  # three.js PropertyBinding reserves [ ] . : /, the viewer uses | as delimiter

# OCP GeomAbs types, as ocp-tessellate reports them (shown in the viewer's properties panel)
PLANE, CYLINDER, SPHERE, OTHER = 0, 1, 3, 10
LINE, CIRCLE = 0, 1


def wxyz_to_xyzw(q):
    return [float(q[1]), float(q[2]), float(q[3]), float(q[0])]


def safe(name: str) -> str:
    return _RESERVED.sub("_", name)


def select_geoms(model: mujoco.MjModel, which: str = "visual", groups=None) -> list[int]:
    """Geom ids to show: `visual` (groups 0-2, MuJoCo's default), `collision` (contype or
    conaffinity set), `all`, or an explicit list of geom groups."""
    if groups is not None:
        return [g for g in range(model.ngeom) if model.geom_group[g] in groups]
    if which == "visual":
        return [g for g in range(model.ngeom) if model.geom_group[g] in VISUAL_GROUPS]
    if which == "collision":
        return [g for g in range(model.ngeom) if model.geom_contype[g] or model.geom_conaffinity[g]]
    if which == "all":
        return list(range(model.ngeom))
    raise ValueError(which)


def geom_rgba(model, g):
    rgba = model.geom_rgba[g]
    mat = model.geom_matid[g]
    if mat >= 0 and np.allclose(rgba, DEFAULT_RGBA):
        rgba = model.mat_rgba[mat]
    return rgba


def web_color(rgba) -> str:
    r, g, b = (int(round(float(c) * 255)) for c in rgba[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


# ----------------------------------------------------------------------------------------------
# Direct tessellations (numpy). Each returns the nine arrays three-cad-viewer's decoder expects.
# ----------------------------------------------------------------------------------------------


def _tess(vertices, normals, triangles, faces, segments=(), edge_kinds=(), obj_vertices=()):
    """`faces` is a list of (face type, triangle count) in triangle order; `segments` a list of
    line segments ((a, b) pairs) and `edge_kinds` a list of (edge type, segment count) in order."""
    segments = np.asarray(segments, dtype=np.float32).reshape(-1, 2, 3)
    assert sum(n for _, n in edge_kinds) == len(segments), "segments and edge_kinds disagree"
    assert sum(n for _, n in faces) * 3 == np.asarray(triangles).size, "faces and triangles disagree"
    return {
        "vertices": np.asarray(vertices, dtype=np.float32).ravel(),
        "triangles": np.asarray(triangles, dtype=np.int32).ravel(),
        "normals": np.asarray(normals, dtype=np.float32).ravel(),
        "edges": segments.ravel(),
        "obj_vertices": np.asarray(obj_vertices, dtype=np.float32).ravel(),
        "face_types": np.array([f for f, _ in faces], dtype=np.int32),
        "edge_types": np.array([e for e, _ in edge_kinds], dtype=np.int32),
        "triangles_per_face": np.array([n for _, n in faces], dtype=np.int32),
        "segments_per_edge": np.array([n for _, n in edge_kinds], dtype=np.int32),
    }


def _circle(r, z, n):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.stack([r * np.cos(a), r * np.sin(a), np.full(n, z)], axis=1)


def box_tess(hx, hy, hz):
    verts, norms, tris, faces = [], [], [], []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            n = np.zeros(3)
            n[axis] = sign
            u, v = [i for i in range(3) if i != axis]
            h = np.array([hx, hy, hz])
            quad = []
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = np.zeros(3)
                p[axis] = sign * h[axis]
                p[u] = su * h[u]
                p[v] = sv * h[v]
                quad.append(p)
            base = len(verts)
            verts += quad
            norms += [n] * 4
            # counter-clockwise seen from outside
            if np.dot(np.cross(quad[1] - quad[0], quad[2] - quad[0]), n) > 0:
                tris += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
            else:
                tris += [[base, base + 2, base + 1], [base, base + 3, base + 2]]
            faces.append((PLANE, 2))
    corners = np.array([[sx * hx, sy * hy, sz * hz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    edges = []
    for i, a in enumerate(corners):
        for b in corners[i + 1 :]:
            if np.count_nonzero(np.abs(a - b) > 1e-12) == 1:
                edges.append([a, b])
    return _tess(verts, norms, tris, faces, edges, [(LINE, 1)] * len(edges), corners)


def _ring_strip(rings, ring_normals):
    """Triangles joining consecutive rings of equal size (each ring a closed loop)."""
    verts = np.concatenate(rings)
    norms = np.concatenate(ring_normals)
    n = len(rings[0])
    tris = []
    for k in range(len(rings) - 1):
        a0, b0 = k * n, (k + 1) * n
        for i in range(n):
            j = (i + 1) % n
            tris += [[a0 + i, a0 + j, b0 + j], [a0 + i, b0 + j, b0 + i]]
    return verts, norms, tris


def sphere_tess(r, n_lon=48, n_lat=24):
    lats = np.linspace(-np.pi / 2, np.pi / 2, n_lat + 1)
    rings, normals = [], []
    for lat in lats:
        ring = _circle(np.cos(lat), np.sin(lat), n_lon)
        normals.append(ring)
        rings.append(ring * r)
    v, nrm, t = _ring_strip(rings, normals)
    return _tess(v, nrm, t, [(SPHERE, len(t))])


def cylinder_tess(r, hh, n=64):
    side_rings = [_circle(r, -hh, n), _circle(r, hh, n)]
    side_norms = [_circle(1.0, 0.0, n)] * 2
    v, nrm, t = _ring_strip(side_rings, side_norms)
    verts, norms, tris = list(v), list(nrm), list(t)
    faces = [(CYLINDER, len(t))]
    for z, sign in ((-hh, -1.0), (hh, 1.0)):
        base = len(verts)
        verts.append([0.0, 0.0, z])
        verts += list(_circle(r, z, n))
        norms += [[0.0, 0.0, sign]] * (n + 1)
        cap = []
        for i in range(n):
            j = (i + 1) % n
            cap.append([base, base + 1 + i, base + 1 + j] if sign > 0 else [base, base + 1 + j, base + 1 + i])
        tris += cap
        faces.append((PLANE, len(cap)))
    edges = []
    for z in (-hh, hh):
        c = _circle(r, z, n)
        edges += [[c[i], c[(i + 1) % n]] for i in range(n)]
    return _tess(verts, norms, tris, faces, edges, [(CIRCLE, n), (CIRCLE, n)])


def capsule_tess(r, hh, n=48, n_cap=12):
    rings, normals = [], []
    for lat in np.linspace(-np.pi / 2, 0, n_cap + 1):  # lower hemisphere
        unit = _circle(np.cos(lat), np.sin(lat), n)
        rings.append(unit * r + [0, 0, -hh])
        normals.append(unit)
    for lat in np.linspace(0, np.pi / 2, n_cap + 1):  # upper hemisphere
        unit = _circle(np.cos(lat), np.sin(lat), n)
        rings.append(unit * r + [0, 0, hh])
        normals.append(unit)
    v, nrm, t = _ring_strip(rings, normals)
    return _tess(v, nrm, t, [(OTHER, len(t))])


def plane_tess(hx, hy):
    v = [[-hx, -hy, 0], [hx, -hy, 0], [hx, hy, 0], [-hx, hy, 0]]
    edges = [[v[i], v[(i + 1) % 4]] for i in range(4)]
    return _tess(v, [[0, 0, 1]] * 4, [[0, 1, 2], [0, 2, 3]], [(PLANE, 2)], edges, [(LINE, 1)] * 4, v)


def mesh_tess(model, mesh_id):
    """A MuJoCo mesh asset as a raw leaf: its own vertices, faces and normals, in mm, no edges."""
    va, vn = model.mesh_vertadr[mesh_id], model.mesh_vertnum[mesh_id]
    fa, fn = model.mesh_faceadr[mesh_id], model.mesh_facenum[mesh_id]
    na, nn = model.mesh_normaladr[mesh_id], model.mesh_normalnum[mesh_id]
    vert = model.mesh_vert[va : va + vn].astype(np.float64) * MM
    face = model.mesh_face[fa : fa + fn]
    fnorm = model.mesh_facenormal[fa : fa + fn]
    normal = model.mesh_normal[na : na + nn]
    if nn == vn and np.array_equal(face, fnorm):
        norms = normal  # one normal per vertex, indexed like the vertices: use as is
        verts, tris = vert, face
    else:
        # normals indexed separately: split vertices into unique (vertex, normal) pairs
        pairs = np.stack([face.ravel(), fnorm.ravel()], axis=1)
        uniq, inverse = np.unique(pairs, axis=0, return_inverse=True)
        verts, norms, tris = vert[uniq[:, 0]], normal[uniq[:, 1]], inverse.reshape(-1, 3)
    return _tess(verts, norms, tris, [(OTHER, len(tris))])


# ----------------------------------------------------------------------------------------------
# build123d primitives, tessellated by ocp-tessellate exactly as `show` would
# ----------------------------------------------------------------------------------------------


def brep_solid(gtype, size_mm, plane_half=PLANE_HALF_MM):
    from build123d import Box, Cylinder, Location, Rectangle, Sphere

    if gtype == mujoco.mjtGeom.mjGEOM_BOX:
        return Box(*(2 * s for s in size_mm))
    if gtype == mujoco.mjtGeom.mjGEOM_SPHERE:
        return Sphere(size_mm[0])
    if gtype == mujoco.mjtGeom.mjGEOM_CYLINDER:
        return Cylinder(size_mm[0], 2 * size_mm[1])
    if gtype == mujoco.mjtGeom.mjGEOM_CAPSULE:
        r, hh = size_mm[0], size_mm[1]
        return Cylinder(r, 2 * hh).fuse(Sphere(r).moved(Location((0, 0, hh)))).fuse(Sphere(r).moved(Location((0, 0, -hh))))
    if gtype == mujoco.mjtGeom.mjGEOM_PLANE:
        hx = size_mm[0] if size_mm[0] > 0 else plane_half
        hy = size_mm[1] if size_mm[1] > 0 else plane_half
        return Rectangle(2 * min(hx, plane_half), 2 * min(hy, plane_half)).face()
    raise NotImplementedError(f"geom type {gtype}")


def brep_tess(solid, key: str, deviation=0.1, angular_tolerance=0.2):
    """`key` must be unique per distinct solid: ocp-tessellate caches meshes by it."""
    from ocp_tessellate.ocp_utils import bounding_box
    from ocp_tessellate.tessellator import compute_quality, tessellate

    bb = bounding_box(solid.wrapped, loc=None, optimal=False)
    quality = compute_quality(bb, deviation=deviation)
    cache_key = hashlib.sha256(f"s5-sim-primitive:{key}".encode()).hexdigest()
    mesh = tessellate(solid.wrapped, cache_key, deviation=deviation, quality=quality, angular_tolerance=angular_tolerance, compute_edges=True)
    return {k: np.asarray(v) for k, v in mesh.items()}


# ----------------------------------------------------------------------------------------------
# The scene
# ----------------------------------------------------------------------------------------------


@dataclass
class SimScene:
    name: str
    root: str  # "/<name>"
    message: dict  # the "data" message, without config (see message_for)
    body_paths: dict[int, str]  # body id -> group path (only bodies that have a leaf)
    leaves: dict[str, dict]  # leaf path -> {geom, geom_name, body, body_name, kind}
    base_pos_mm: dict[int, list[float]]  # body id -> group position at the base pose
    base_quat_xyzw: dict[int, list[float]]
    stats: dict = field(default_factory=dict)

    def pick_to_geom(self, path: str, name: str) -> dict | None:
        """A pick (`lastPick` path and name) back to the MuJoCo geom and body."""
        return self.leaves.get(f"{path}/{name}")


def body_poses(model, data):
    return data.xpos.copy(), data.xquat.copy()


def build_scene(model, data=None, *, which="visual", groups=None, primitives="direct", name="sim",
                plane_half_mm=PLANE_HALF_MM) -> SimScene:
    """Scene for `model` at the pose in `data` (default: qpos0 after mj_kinematics)."""
    t_start = time.perf_counter()
    timings = {}
    if data is None:
        data = mujoco.MjData(model)
        mujoco.mj_kinematics(model, data)
    xpos, xquat = body_poses(model, data)
    geoms = select_geoms(model, which, groups)
    root = f"/{safe(name)}"

    instances, instance_keys = [], {}
    tess_time = {"direct": 0.0, "brep": 0.0, "mesh": 0.0}
    counts = {"direct": 0, "brep": 0, "mesh": 0}

    def instance_for(g):
        gtype = int(model.geom_type[g])
        if gtype == mujoco.mjtGeom.mjGEOM_MESH:
            mid = int(model.geom_dataid[g])
            va, vn = model.mesh_vertadr[mid], model.mesh_vertnum[mid]
            fa, fn = model.mesh_faceadr[mid], model.mesh_facenum[mid]
            key = ("mesh", hashlib.sha1(model.mesh_vert[va : va + vn].tobytes() + model.mesh_face[fa : fa + fn].tobytes()).hexdigest())
            kind = "mesh"
        else:
            size_mm = tuple(round(float(s) * MM, 6) for s in model.geom_size[g])
            key = (primitives, gtype, size_mm)
            kind = primitives
        if key in instance_keys:
            return instance_keys[key], kind
        t0 = time.perf_counter()
        if kind == "mesh":
            tess = mesh_tess(model, mid)
        elif kind == "brep":
            tess = brep_tess(brep_solid(gtype, size_mm, plane_half_mm), repr((gtype, size_mm, plane_half_mm)))
        else:
            hx, hy, hz = size_mm
            tess = {
                mujoco.mjtGeom.mjGEOM_BOX: lambda: box_tess(hx, hy, hz),
                mujoco.mjtGeom.mjGEOM_SPHERE: lambda: sphere_tess(hx),
                mujoco.mjtGeom.mjGEOM_CYLINDER: lambda: cylinder_tess(hx, hy),
                mujoco.mjtGeom.mjGEOM_CAPSULE: lambda: capsule_tess(hx, hy),
                mujoco.mjtGeom.mjGEOM_PLANE: lambda: plane_tess(min(hx or plane_half_mm, plane_half_mm), min(hy or plane_half_mm, plane_half_mm)),
            }[gtype]()
        tess_time[kind] += time.perf_counter() - t0
        counts[kind] += 1
        instance_keys[key] = len(instances)
        instances.append(tess)
        return instance_keys[key], kind

    body_parts: dict[int, list] = {}
    leaves = {}
    used_names: dict[int, set] = {}
    for g in geoms:
        b = int(model.geom_bodyid[g])
        ref, kind = instance_for(g)
        gname = model.geom(g).name
        if gname:
            leaf = safe(gname)
        elif model.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH:
            leaf = f"g{g}_{safe(model.mesh(int(model.geom_dataid[g])).name)}"
        else:
            leaf = f"g{g}_{mujoco.mjtGeom(int(model.geom_type[g])).name[7:].lower()}"
        names = used_names.setdefault(b, set())
        if leaf in names:
            leaf = f"{leaf}_g{g}"
        names.add(leaf)
        bname = safe(model.body(b).name) or f"body{b}"
        path = f"{root}/{bname}/{leaf}"
        rgba = geom_rgba(model, g)
        body_parts.setdefault(b, []).append(
            {
                "id": path,
                "type": "shapes",
                "subtype": "faces" if model.geom_type[g] == mujoco.mjtGeom.mjGEOM_PLANE else "solid",
                "name": leaf,
                "shape": {"ref": ref},
                "state": [1, 1],
                "color": web_color(rgba),
                "alpha": float(rgba[3]),
                "material": None,
                "normalize_uvs": True,
                "texture": None,
                "loc": [[float(v) * MM for v in model.geom_pos[g]], wxyz_to_xyzw(model.geom_quat[g])],
                "renderback": bool(model.geom_type[g] == mujoco.mjtGeom.mjGEOM_PLANE),
                "accuracy": None,
                "bb": None,
            }
        )
        leaves[path] = {"geom": g, "geom_name": gname, "body": b, "body_name": model.body(b).name, "kind": kind,
                        "type": mujoco.mjtGeom(int(model.geom_type[g])).name}
    timings["tessellate_s"] = time.perf_counter() - t_start

    t0 = time.perf_counter()
    body_paths, base_pos, base_quat, parts = {}, {}, {}, []
    for b in sorted(body_parts):
        bname = safe(model.body(b).name) or f"body{b}"
        path = f"{root}/{bname}"
        body_paths[b] = path
        base_pos[b] = [float(v) * MM for v in xpos[b]]
        base_quat[b] = wxyz_to_xyzw(xquat[b])
        parts.append({"version": 3, "parts": body_parts[b], "loc": [base_pos[b], base_quat[b]], "name": bname, "id": path})

    # bounding box of every leaf at the base pose (instance AABB corners, transformed)
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for b, items in body_parts.items():
        Rb = np.zeros(9)
        mujoco.mju_quat2Mat(Rb, xquat[b])
        Rb = Rb.reshape(3, 3)
        for item in items:
            v = instances[item["shape"]["ref"]]["vertices"].reshape(-1, 3)
            if not len(v):
                continue
            mn, mx = v.min(0), v.max(0)
            corners = np.array([[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])])
            q = item["loc"][1]
            Rg = np.zeros(9)
            mujoco.mju_quat2Mat(Rg, np.array([q[3], q[0], q[1], q[2]]))
            world = (corners @ Rg.reshape(3, 3).T + item["loc"][0]) @ Rb.T + xpos[b] * MM
            lo, hi = np.minimum(lo, world.min(0)), np.maximum(hi, world.max(0))
    bb = {"xmin": lo[0], "xmax": hi[0], "ymin": lo[1], "ymax": hi[1], "zmin": lo[2], "zmax": hi[2]}
    shapes = {"version": 3, "parts": parts, "loc": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]], "name": safe(name), "id": root,
              "normal_len": 0, "bb": {k: float(v) for k, v in bb.items()}}
    timings["tree_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    from ocp_tessellate.utils import numpy_to_buffer_json

    data_part = numpy_to_buffer_json({"instances": instances, "shapes": shapes})
    timings["encode_s"] = time.perf_counter() - t0
    timings["total_s"] = time.perf_counter() - t_start

    drawn_tris = sum(len(instances[i["shape"]["ref"]]["triangles"]) // 3 for items in body_parts.values() for i in items)
    stats = {
        "which": which if groups is None else f"groups {list(groups)}",
        "primitives": primitives,
        "bodies": len(body_parts),
        "leaves": len(leaves),
        "instances": len(instances),
        "instances_by_kind": counts,
        "tessellation_s_by_kind": tess_time,
        "unique_vertices": int(sum(len(t["vertices"]) // 3 for t in instances)),
        "unique_triangles": int(sum(len(t["triangles"]) // 3 for t in instances)),
        "drawn_triangles": int(drawn_tris),
        "timings": timings,
        "bb_mm": shapes["bb"],
    }
    message = {"data": data_part, "type": "data", "count": len(leaves)}
    return SimScene(safe(name), root, message, body_paths, leaves, base_pos, base_quat, stats)


def message_for(scene: SimScene, config: dict, reset_camera: str | None = "reset") -> dict:
    """The full wire message: the scene plus a config taken from the viewer's own pipeline."""
    cfg = dict(config)
    if reset_camera is not None:
        cfg["reset_camera"] = reset_camera
    return {**scene.message, "config": cfg}


def with_body_locs(scene: SimScene, xpos, xquat) -> dict:
    """The scene's data part with every body group moved to (xpos, xquat); geometry unchanged."""
    shapes = dict(scene.message["data"]["shapes"])
    parts = []
    by_path = {p: b for b, p in scene.body_paths.items()}
    for node in shapes["parts"]:
        b = by_path[node["id"]]
        parts.append({**node, "loc": [[float(v) * MM for v in xpos[b]], wxyz_to_xyzw(xquat[b])]})
    shapes["parts"] = parts
    return {**scene.message, "data": {**scene.message["data"], "shapes": shapes}}
