"""Printed bracket: a base flange bolted to the plate, with an upright arm that
carries the hinge pin.

Bracket frame: origin at the centre of the base's bottom face, so placing it at
the plate's own origin sits it directly on the plate's top face. x along the
plate's length, z up.
"""

from build123d import Part

from minimal.params import BracketParams, PinParams
from minimal.shapes import bore, box_between

MATERIAL = "PETG"
OVERLAP = 1.0  # mm a cut reaches past a face, so the boolean leaves no skin


def base_bolt_holes(bracket: BracketParams) -> list[tuple[float, float]]:
    """Centres of the two base bolt holes, (x, y) in the bracket frame."""
    return [(0.0, side * bracket.bolt_spacing / 2) for side in (-1, 1)]


def pin_axis(bracket: BracketParams) -> tuple[float, float, float]:
    """Centre of the pin, (x, y, z) in the bracket frame."""
    arm_x = bracket.base_length / 2 - bracket.arm_thickness / 2
    return (arm_x, 0.0, bracket.base_thickness + bracket.pin_height)


def make(bracket: BracketParams, pin: PinParams) -> Part:
    half_x, half_y = bracket.base_length / 2, bracket.base_width / 2
    base = box_between((-half_x, half_x), (-half_y, half_y), (0, bracket.base_thickness))
    arm_x0 = half_x - bracket.arm_thickness
    arm_top = bracket.base_thickness + bracket.arm_height
    arm = box_between((arm_x0, half_x), (-half_y, half_y), (bracket.base_thickness, arm_top))
    part = base + arm

    for x, y in base_bolt_holes(bracket):
        part -= bore("z", bracket.bolt_clearance, (x, y, -OVERLAP), bracket.base_thickness + 2 * OVERLAP)

    px, py, pz = pin_axis(bracket)
    hole_diameter = pin.diameter + 2 * bracket.pin_clearance
    bore_start = (px - bracket.arm_thickness / 2 - OVERLAP, py, pz)
    part -= bore("x", hole_diameter, bore_start, bracket.arm_thickness + 2 * OVERLAP)

    part.label = "bracket"
    return part
