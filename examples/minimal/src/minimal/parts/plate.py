"""Flat aluminium mounting plate: four corner bolt holes, filleted corners.

Plate frame: origin at the centre of the plate's top face, x along its length, z up.
"""

from build123d import Circle, Part, Polygon, Pos, extrude, fillet

from minimal.params import PlateParams

MATERIAL = "aluminium sheet"


def mount_bolt_holes(plate: PlateParams) -> list[tuple[float, float]]:
    """Centres of the four corner bolt holes, (x, y) in the plate frame."""
    x = plate.length / 2 - plate.hole_margin
    y = plate.width / 2 - plate.hole_margin
    return [(sx * x, sy * y) for sx in (-1, 1) for sy in (-1, 1)]


def make(plate: PlateParams) -> Part:
    half_x, half_y = plate.length / 2, plate.width / 2
    corners = [(-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)]
    profile = Polygon(*corners, align=None)
    profile = fillet(profile.vertices(), plate.corner_radius)
    for x, y in mount_bolt_holes(plate):
        profile -= Pos(x, y) * Circle(plate.hole_diameter / 2)

    part = Pos(0, 0, -plate.thickness) * extrude(profile, plate.thickness)
    part.label = "plate"
    return part
