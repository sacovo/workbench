"""Each part is a single valid solid, its holes are where the layout functions say, and
nothing in the assembly interferes."""

import itertools

import pytest
from build123d import GeomType

from minimal.assembly import minimal_assembly
from minimal.params import BRACKET, PIN, PLATE
from minimal.parts import bracket, pin, plate


@pytest.fixture(scope="module")
def parts():
    return {
        "plate": plate.make(PLATE),
        "bracket": bracket.make(BRACKET, PIN),
        "pin": pin.make(PIN),
    }


def test_each_part_is_one_valid_solid(parts):
    for name, part in parts.items():
        assert part.is_valid, name
        assert len(part.solids()) == 1, name


def test_plate_holes_are_cylindrical_faces_at_mount_bolt_holes(parts):
    """Independent measurement: real cylindrical faces of the right radius at the right
    centres, not a re-check of the function that placed them."""
    expected = {(round(x, 2), round(y, 2)) for x, y in plate.mount_bolt_holes(PLATE)}
    assert len(expected) == 4

    cylindrical = [
        f for f in parts["plate"].faces().filter_by(GeomType.CYLINDER) if f.radius == pytest.approx(PLATE.hole_diameter / 2)
    ]
    found = {(round(f.axis_of_rotation.position.X, 2), round(f.axis_of_rotation.position.Y, 2)) for f in cylindrical}
    assert found == expected


def test_bracket_pin_hole_is_centred_on_pin_axis(parts):
    px, py, pz = bracket.pin_axis(BRACKET)
    hole_radius = PIN.diameter / 2 + BRACKET.pin_clearance
    cylindrical = [f for f in parts["bracket"].faces().filter_by(GeomType.CYLINDER) if f.radius == pytest.approx(hole_radius)]
    positions = {
        (round(f.axis_of_rotation.position.Y, 2), round(f.axis_of_rotation.position.Z, 2)) for f in cylindrical
    }
    assert positions == {(round(py, 2), round(pz, 2))}


def test_no_parts_interfere_in_the_assembly():
    children = {child.label: child for child in minimal_assembly().children}
    assert len(children) == 3
    for (name_a, a), (name_b, b) in itertools.combinations(children.items(), 2):
        box_a, box_b = a.bounding_box(), b.bounding_box()
        if any(getattr(box_a.min, k) > getattr(box_b.max, k) or getattr(box_b.min, k) > getattr(box_a.max, k) for k in "XYZ"):
            continue  # bounding boxes do not even overlap
        overlap = a & b
        assert (overlap.volume if overlap else 0.0) == pytest.approx(0, abs=1e-6), f"{name_a} overlaps {name_b}"
