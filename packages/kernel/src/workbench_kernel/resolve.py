"""Viewer pick ids to parts and exact properties, in world and part-local coordinates.

An id is what the stock viewer reports: `/<root>/<part>` for a part (double-click),
`/<root>/<part>/faces/faces_<i>` (or `edges/edges_<i>`, `vertices/vertices_<i>`) for a sub-shape
picked by a measure tool. It names a shape in the scene's backend table, which sits where the
viewer draws it ("world").

Part-local properties are computed on that shape moved by the inverse of the part's placement
(root location times the child's own location), never on a `located(Location())` copy: a
sub-shape carries its own location inside the part, and replacing the whole location would
drop it too.

Properties by kind:
- face: geom_type, area, centre (area centroid), and either `normal` (planes and free-form
  surfaces, at the middle of the face's parameter range, pointing out of the material) or `axis`
  (cylinder, cone, sphere, torus, revolution) with `radius` where the surface has one;
- edge: geom_type, length, centre (length centroid), start, end, and `axis` (line: through the
  start along the edge; circle and ellipse: through the centre, normal to their plane) with
  `radius` for a circle;
- vertex: point (also given as centre);
- solid, compound, shell: volume, area, centre (volume centroid, or area centroid without
  volume), bbox.
"""

from __future__ import annotations

import math

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_CurveType as C
from OCP.GeomAbs import GeomAbs_SurfaceType as S
from OCP.gp import gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_ShapeEnum as T
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from workbench_kernel.scene import Scene, bbox_of

_SURFACES = {
    S.GeomAbs_Plane: "plane", S.GeomAbs_Cylinder: "cylinder", S.GeomAbs_Cone: "cone",
    S.GeomAbs_Sphere: "sphere", S.GeomAbs_Torus: "torus", S.GeomAbs_BezierSurface: "bezier",
    S.GeomAbs_BSplineSurface: "bspline", S.GeomAbs_SurfaceOfRevolution: "revolution",
    S.GeomAbs_SurfaceOfExtrusion: "extrusion", S.GeomAbs_OffsetSurface: "offset",
    S.GeomAbs_OtherSurface: "other",
}
_CURVES = {
    C.GeomAbs_Line: "line", C.GeomAbs_Circle: "circle", C.GeomAbs_Ellipse: "ellipse",
    C.GeomAbs_Hyperbola: "hyperbola", C.GeomAbs_Parabola: "parabola", C.GeomAbs_BezierCurve: "bezier",
    C.GeomAbs_BSplineCurve: "bspline", C.GeomAbs_OffsetCurve: "offset", C.GeomAbs_OtherCurve: "other",
}
_KINDS = {
    T.TopAbs_VERTEX: "vertex", T.TopAbs_EDGE: "edge", T.TopAbs_WIRE: "wire", T.TopAbs_FACE: "face",
    T.TopAbs_SHELL: "shell", T.TopAbs_SOLID: "solid", T.TopAbs_COMPSOLID: "compsolid", T.TopAbs_COMPOUND: "compound",
}


def _xyz(p) -> list[float]:
    return [p.X(), p.Y(), p.Z()]


def _unit(v) -> list[float]:
    n = math.sqrt(v.X() ** 2 + v.Y() ** 2 + v.Z() ** 2)
    return [v.X() / n, v.Y() / n, v.Z() / n]


def _axis(ax1) -> dict:
    return {"origin": _xyz(ax1.Location()), "direction": _unit(ax1.Direction())}


def _face(face) -> dict:
    surface = BRepAdaptor_Surface(face)  # applies the face's location
    kind = surface.GetType()
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    out = {"geom_type": _SURFACES.get(kind, "other"), "area": props.Mass(), "centre": _xyz(props.CentreOfMass())}
    if kind == S.GeomAbs_Cylinder:
        out["axis"], out["radius"] = _axis(surface.Cylinder().Axis()), surface.Cylinder().Radius()
    elif kind == S.GeomAbs_Cone:
        cone = surface.Cone()
        out["axis"], out["radius"], out["semi_angle_deg"] = _axis(cone.Axis()), cone.RefRadius(), math.degrees(cone.SemiAngle())
    elif kind == S.GeomAbs_Sphere:
        sphere = surface.Sphere()
        out["axis"], out["radius"] = _axis(sphere.Position().Axis()), sphere.Radius()
    elif kind == S.GeomAbs_Torus:
        torus = surface.Torus()
        out["axis"], out["major_radius"], out["minor_radius"] = _axis(torus.Axis()), torus.MajorRadius(), torus.MinorRadius()
    elif kind == S.GeomAbs_SurfaceOfRevolution:
        out["axis"] = _axis(surface.AxeOfRevolution())
    else:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        point, normal = gp_Pnt(), gp_Vec()
        BRepGProp_Face(face).Normal((u0 + u1) / 2, (v0 + v1) / 2, point, normal)  # reversed with the face
        out["normal"] = _unit(normal)
    return out


def _edge(edge) -> dict:
    start = _xyz(BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge, True)))
    end = _xyz(BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge, True)))
    if BRep_Tool.Degenerated_s(edge):
        return {"geom_type": "degenerated", "length": 0.0, "centre": start, "start": start, "end": end}
    curve = BRepAdaptor_Curve(edge)  # applies the edge's location
    kind = curve.GetType()
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    out = {"geom_type": _CURVES.get(kind, "other"), "length": props.Mass(), "centre": _xyz(props.CentreOfMass()), "start": start, "end": end}
    if kind == C.GeomAbs_Line:
        d = [e - s for s, e in zip(start, end, strict=True)]
        n = math.sqrt(sum(x * x for x in d))
        out["axis"] = {"origin": start, "direction": [x / n for x in d]}
    elif kind == C.GeomAbs_Circle:
        out["axis"], out["radius"] = _axis(curve.Circle().Axis()), curve.Circle().Radius()
    elif kind == C.GeomAbs_Ellipse:
        ellipse = curve.Ellipse()
        out["axis"], out["major_radius"], out["minor_radius"] = _axis(ellipse.Axis()), ellipse.MajorRadius(), ellipse.MinorRadius()
    return out


def _body(shape) -> dict:
    volume, area = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume)
    BRepGProp.SurfaceProperties_s(shape, area)
    centre = volume.CentreOfMass() if volume.Mass() > 0 else area.CentreOfMass()
    return {"volume": volume.Mass(), "area": area.Mass(), "centre": _xyz(centre), "bbox": bbox_of([shape])}


def kind_of(shape) -> str:
    return _KINDS[shape.ShapeType()]


def properties(shape) -> dict:
    """The properties of one OCCT shape in the coordinates it is placed in."""
    kind = shape.ShapeType()
    if kind == T.TopAbs_FACE:
        return _face(TopoDS.Face(shape))
    if kind == T.TopAbs_EDGE:
        return _edge(TopoDS.Edge(shape))
    if kind == T.TopAbs_VERTEX:
        point = _xyz(BRep_Tool.Pnt_s(TopoDS.Vertex(shape)))
        return {"geom_type": "point", "point": point, "centre": point}
    return _body(shape)


def resolve(scene: Scene, shape_id) -> dict:
    """One viewer id resolved against a scene; `found` is False with a reason when it cannot be."""
    if not isinstance(shape_id, str):
        return {"id": shape_id, "found": False, "error": "a pick id is a string"}
    part = scene.part_of(shape_id)
    shape = scene.model.get(shape_id)
    if part is None or shape is None:
        return {"id": shape_id, "found": False, "error": f"scene {scene.id} has no shape {shape_id!r}"}
    local = shape.Moved(part.placement.Inverted())
    return {
        "id": shape_id,
        "found": True,
        "part": {"label": part.label, "path": part.path},
        "kind": kind_of(shape),
        "world": properties(shape),
        "local": properties(local),
    }
