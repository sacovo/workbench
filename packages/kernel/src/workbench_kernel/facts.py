"""Measured facts per part of a built assembly: volume, exact bounding box, BRep validity."""

from __future__ import annotations

from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

from workbench_kernel.scene import Draft, bbox_of


def part_facts(draft: Draft) -> dict:
    """Facts of one part. The box is in the viewer's world: the part's own top-level location is
    replaced by its placement (root location times that location), which moves it by the root."""
    shape = draft.shape.wrapped
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return {
        "label": draft.label,
        "fingerprint": draft.fingerprint,
        "volume": props.Mass(),
        "bbox": bbox_of([shape.Located(draft.placement)]),
        "is_valid": bool(BRepCheck_Analyzer(shape).IsValid()),
    }
