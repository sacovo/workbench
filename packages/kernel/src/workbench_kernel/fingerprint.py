"""Geometry identity of a part, independent of where it is placed.

The hash covers the part's ASCII BREP with its location stripped at the OCC level, without
triangulation, and without the 7-character TShape flag lines. Spike S1 measured why the flags
must go: tessellating a shape clears its "Modified" flag (0111000 to 0101000), so a plain BREP
hash flagged 50 unchanged robot parts as changed. A part moved by its assembly keeps its
fingerprint; a part whose geometry changed gets a new one.
"""

from __future__ import annotations

import hashlib
import io
import re

from OCP.BRepTools import BRepTools
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_FormatVersion

# A TShape's flags (free, modified, checked, orientable, closed, infinite, convex) on a line of their own.
_FLAG_LINE = re.compile(rb"^[01]{7}$", re.MULTILINE)


def fingerprint(shape) -> str:
    """sha256 of the unplaced, untriangulated ASCII BREP of `shape` (build123d or TopoDS) without flag lines."""
    topods = getattr(shape, "wrapped", shape)
    unplaced = topods.Located(TopLoc_Location())
    buffer = io.BytesIO()
    BRepTools.Write_s(unplaced, buffer, False, False, TopTools_FormatVersion.TopTools_FormatVersion_VERSION_1)
    return hashlib.sha256(_FLAG_LINE.sub(b"", buffer.getvalue())).hexdigest()
