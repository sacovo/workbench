"""A fingerprint identifies geometry: not placement, and not what tessellation does to a shape."""

import hashlib
import io

from build123d import Box, Compound, Pos, Rot
from OCP.BRepTools import BRepTools
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_FormatVersion

from workbench_kernel.fingerprint import fingerprint
from workbench_kernel.host import ViewerHost


def _plain_brep_hash(shape) -> str:
    """What a naive fingerprint would hash: the same BREP, flag lines included."""
    buffer = io.BytesIO()
    BRepTools.Write_s(shape.wrapped.Located(TopLoc_Location()), buffer, False, False, TopTools_FormatVersion.TopTools_FormatVersion_VERSION_1)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def test_showing_a_part_leaves_its_fingerprint_alone():
    part = Box(10, 20, 30) - Pos(0, 0, 10) * Box(4, 4, 20)
    part.label = "part"
    plain, before = _plain_brep_hash(part), fingerprint(part)

    ViewerHost().show(Compound(label="assembly", children=[part]))

    assert _plain_brep_hash(part) != plain  # the tessellation did change the BREP's flags (spike S1, fix 3)
    assert fingerprint(part) == before


def test_placement_does_not_change_a_fingerprint_and_geometry_does():
    part = Box(10, 20, 30)
    assert fingerprint(Pos(5, 6, 7) * Rot(0, 0, 30) * part) == fingerprint(part)
    assert fingerprint(Box(10, 20, 30.001)) != fingerprint(part)
