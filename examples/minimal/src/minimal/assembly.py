"""CAD assembly of the minimal example, built from its parts and their frames."""

from build123d import Compound, Pos

from minimal.params import BRACKET, PIN, PLATE
from minimal.parts import bracket, pin, plate


def minimal_assembly(plate_params=PLATE, bracket_params=BRACKET, pin_params=PIN) -> Compound:
    """Plate at the base, bracket bolted to its top face, pin through the bracket's arm."""
    pin_part = Pos(*bracket.pin_axis(bracket_params)) * pin.make(pin_params)
    pin_part.label = "pin"  # defensive: a moved shape is not guaranteed to keep its label

    children = [
        plate.make(plate_params),
        bracket.make(bracket_params, pin_params),
        pin_part,
    ]
    return Compound(label="minimal_assembly", children=children)
