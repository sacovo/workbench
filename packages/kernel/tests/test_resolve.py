"""Pick ids resolve to their part and its exact properties, in world and part-local coordinates.

Expected values come from the example's own geometry built in this process, never from the
kernel's answers.
"""

from __future__ import annotations

import pytest
from build123d import Axis
from minimal.assembly import minimal_assembly
from minimal.params import BRACKET, PIN, PLATE
from minimal.parts import bracket, plate
from OCP.gp import gp_Pnt, gp_Vec


@pytest.fixture(scope="module")
def built(minimal_kernel):
    return minimal_kernel.ok("build", assembly="minimal")


@pytest.fixture(scope="module")
def children():
    return {c.label: c for c in minimal_assembly().children}


def _part(built, label):
    return next(p for p in built["parts"] if p["label"] == label)


def _resolve(kernel, built, ids):
    return kernel.ok("resolve", scene=built["scene"], ids=ids)["results"]


def _faces(kernel, built, label):
    part = _part(built, label)
    return _resolve(kernel, built, [f"{part['path']}/faces/faces_{i}" for i in range(part["n_faces"])])


def test_plate_faces_resolve_to_the_plate_with_its_geometry(minimal_kernel, built):
    results = _faces(minimal_kernel, built, "plate")
    expected = plate.make(PLATE)

    assert len(results) == len(expected.faces())
    for r in results:
        assert r["found"] and r["kind"] == "face"
        assert r["part"] == {"label": "plate", "path": _part(built, "plate")["path"]}
    assert sum(r["world"]["area"] for r in results) == pytest.approx(expected.area, rel=1e-9)

    tops = [r for r in results if r["world"].get("normal") == pytest.approx([0, 0, 1])]
    assert len(tops) == 1
    top = tops[0]
    assert top["world"]["geom_type"] == "plane"
    assert top["world"]["area"] == pytest.approx(expected.faces().sort_by(Axis.Z)[-1].area, rel=1e-9)
    assert top["world"]["centre"][2] == pytest.approx(0.0, abs=1e-9)  # plate frame: top face at z = 0
    # plate.make() places its extrusion with Pos(0, 0, -thickness), so in the part's own frame the
    # top face is at +thickness.
    assert top["local"]["centre"][2] == pytest.approx(PLATE.thickness, abs=1e-9)

    holes = [r["world"] for r in results if r["world"]["geom_type"] == "cylinder" and r["world"]["radius"] == pytest.approx(PLATE.hole_diameter / 2)]
    assert sorted((round(h["axis"]["origin"][0], 6), round(h["axis"]["origin"][1], 6)) for h in holes) == sorted(plate.mount_bolt_holes(PLATE))
    assert all(abs(h["axis"]["direction"][2]) == pytest.approx(1.0) for h in holes)


def test_local_coordinates_are_world_through_the_inverse_of_the_child_location(minimal_kernel, built, children):
    for label, child in children.items():
        trsf = child.location.wrapped.Transformation()  # computed here, not by the kernel
        results = _faces(minimal_kernel, built, label)
        assert results, label
        for r in results:
            world, local = r["world"], r["local"]
            centre = gp_Pnt(*local["centre"]).Transformed(trsf)
            assert world["centre"] == pytest.approx([centre.X(), centre.Y(), centre.Z()], abs=1e-9)
            assert world["area"] == pytest.approx(local["area"], rel=1e-12)
            directions = [(world["normal"], local["normal"])] if "normal" in local else []
            directions += [(world["axis"]["direction"], local["axis"]["direction"])] if "axis" in local else []
            assert directions, r["id"]
            for in_world, in_part in directions:
                v = gp_Vec(*in_part).Transformed(trsf)
                assert in_world == pytest.approx([v.X(), v.Y(), v.Z()], abs=1e-12)

    # The pin is made as Rot(0, 90, 0) * Cylinder(...), so the rotation is part of its location:
    # its axis runs along x in the assembly and along z in its own frame.
    cylinder = next(r for r in _faces(minimal_kernel, built, "pin") if r["world"]["geom_type"] == "cylinder")
    px, py, pz = bracket.pin_axis(BRACKET)
    assert cylinder["world"]["radius"] == pytest.approx(PIN.diameter / 2)
    assert [abs(c) for c in cylinder["world"]["axis"]["direction"]] == pytest.approx([1, 0, 0])
    assert cylinder["world"]["axis"]["origin"][1:] == pytest.approx([py, pz], abs=1e-9)
    assert [abs(c) for c in cylinder["local"]["axis"]["direction"]] == pytest.approx([0, 0, 1])
    assert cylinder["local"]["axis"]["origin"][:2] == pytest.approx([0, 0], abs=1e-9)


def test_edges_vertices_and_whole_parts_resolve(minimal_kernel, built, children):
    pin = children["pin"]
    path = _part(built, "pin")["path"]
    edge_ids = [f"{path}/edges/edges_{i}" for i in range(len(pin.edges()))]
    vertex_ids = [f"{path}/vertices/vertices_{i}" for i in range(len(pin.vertices()))]
    unknown = [f"{path}/faces/faces_99", "/elsewhere/part", 42]
    results = _resolve(minimal_kernel, built, [path, *edge_ids, *vertex_ids, *unknown])
    whole, edges, vertices, missing = results[0], results[1 : 1 + len(edge_ids)], results[1 + len(edge_ids) : -3], results[-3:]

    assert whole["kind"] == "solid" and whole["part"]["label"] == "pin"
    assert whole["world"]["volume"] == pytest.approx(pin.volume, rel=1e-9)
    assert whole["world"]["centre"] == pytest.approx(list(bracket.pin_axis(BRACKET)), abs=1e-9)
    assert whole["local"]["centre"] == pytest.approx([0, 0, 0], abs=1e-9)

    assert all(e["kind"] == "edge" for e in edges)
    circles = [e["world"] for e in edges if e["world"]["geom_type"] == "circle"]
    assert len(circles) == 2 and all(c["radius"] == pytest.approx(PIN.diameter / 2) for c in circles)
    assert sorted(c["axis"]["origin"][0] for c in circles) == pytest.approx([bracket.pin_axis(BRACKET)[0] - PIN.length / 2, bracket.pin_axis(BRACKET)[0] + PIN.length / 2])

    assert len(vertices) == len(pin.vertices()) >= 1
    for v in vertices:
        assert v["kind"] == "vertex"
        x, y, z = v["world"]["point"]
        assert ((y - bracket.pin_axis(BRACKET)[1]) ** 2 + (z - bracket.pin_axis(BRACKET)[2]) ** 2) ** 0.5 == pytest.approx(PIN.diameter / 2)

    assert [m["found"] for m in missing] == [False, False, False]


def test_the_stock_measure_tool_gets_its_answer(minimal_kernel, built):
    top_id = next(r["id"] for r in _faces(minimal_kernel, built, "plate") if r["world"].get("normal") == pytest.approx([0, 0, 1]))

    assert minimal_kernel.ok("backend", scene=built["scene"], message={"activeTool": "PropertiesMeasurement"})["reply"] is None
    answer = minimal_kernel.ok("backend", scene=built["scene"], message={"selectedShapeIDs": [top_id, False]})
    reply = answer["reply"]
    assert reply["type"] == "backend_response" and reply["tool_type"] == "PropertiesMeasurement"
    area = next(section["area"] for section in reply["result"] if "area" in section)
    assert area == pytest.approx(plate.make(PLATE).faces().sort_by(Axis.Z)[-1].area, rel=1e-9)

    bad_id = "/minimal_assembly/plate/faces/faces_99"
    unknown = minimal_kernel.ok("backend", scene=built["scene"], message={"selectedShapeIDs": [bad_id, False]})
    assert unknown["reply"] is None and unknown["unknown_ids"] == [bad_id]
    assert minimal_kernel.ok("backend", scene=built["scene"], message={"activeTool": "None"})["reply"] is None
