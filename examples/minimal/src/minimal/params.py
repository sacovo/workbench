"""Design parameters: the only home of shared dimensions.

Units are mm and degrees, like my-robot. Frames follow REP-103: x forward, y
left, z up.

Plate frame: origin at the centre of the plate's top face. The bracket's frame
shares that origin (it sits directly on the plate), and the pin's own frame is
centred on its own axis; `assembly.py` places it at `parts.bracket.pin_axis`.

This is a toy fixture with no purpose beyond exercising the same
parameter -> part -> assembly -> MJCF pipeline as the real robot, small enough
to read in one sitting: a plate bolted down, a printed bracket bolted to the
plate, and a pin through the bracket that a lever hinges on in mjcf.py.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlateParams:
    """Flat aluminium mounting plate, corner bolt holes, filleted corners."""

    length: float = 80.0  # x; big enough to carry the bracket with a comfortable margin
    width: float = 60.0  # y
    thickness: float = 4.0  # mm sheet stock, a common off-the-shelf aluminium thickness
    corner_radius: float = 4.0  # de-burred corner, cosmetic only
    hole_margin: float = 10.0  # hole centres this far in from each edge
    hole_diameter: float = 3.4  # M3 clearance (ISO 273), for the four mounting bolts
    density: float = 2.70e-3  # g/mm^3, aluminium


@dataclass(frozen=True)
class BracketParams:
    """Printed bracket: a base flange bolted to the plate, and an upright arm
    carrying the pin that `mjcf.py` hinges a lever on."""

    base_length: float = 24.0  # x
    base_width: float = 22.0  # y; leaves 5 mm either side of the two base bolts
    base_thickness: float = 6.0  # printed base flange
    arm_thickness: float = 6.0  # x, the wall the pin passes through
    arm_height: float = 26.0  # z above the base's top face
    pin_height: float = 18.0  # z above the base's top face; leaves 8 mm above the hole to arm_height
    bolt_spacing: float = 12.0  # y, between the two base bolts
    bolt_clearance: float = 3.4  # M3 clearance, same bolts as the plate
    pin_clearance: float = 0.3  # radial, so the pin turns freely in the bracket
    density: float = 0.65e-3  # g/mm^3, PETG at roughly half solid (my-robot's figure for printed parts)


@dataclass(frozen=True)
class PinParams:
    """Steel pin through the bracket's arm; mjcf.py hinges a lever on it."""

    diameter: float = 4.0  # mm, a common dowel pin size; sets the bracket's pin-clearance hole
    length: float = 30.0  # assumed; no pin has been bought yet, measure the real one before printing the bracket
    density: float = 7.85e-3  # g/mm^3, mild steel


PLATE = PlateParams()
BRACKET = BracketParams()
PIN = PinParams()
