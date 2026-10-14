"""Steel hinge pin: a plain cylinder through the bracket's arm.

Pin frame: origin at the centre of the pin, axis along x (matches the bore
through the bracket's arm in parts/bracket.py).
"""

from build123d import Cylinder, Part, Rot

from minimal.params import PinParams

MATERIAL = "steel"


def make(pin: PinParams) -> Part:
    part = Rot(0, 90, 0) * Cylinder(pin.diameter / 2, pin.length)
    part.label = "pin"
    return part
