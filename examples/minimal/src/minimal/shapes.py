"""Small geometry helpers shared by the parts modules. Copied from my-robot's
src/robot/shapes.py, which this example otherwise does not depend on."""

from build123d import Box, Cylinder, Part, Pos, Rot

_AXIS_ROTATION = {"x": Rot(0, 90, 0), "y": Rot(90, 0, 0), "z": Rot(0, 0, 0)}


def box_between(x: tuple[float, float], y: tuple[float, float], z: tuple[float, float]) -> Part:
    """Axis-aligned box given its extent on each axis."""
    return Pos(sum(x) / 2, sum(y) / 2, sum(z) / 2) * Box(x[1] - x[0], y[1] - y[0], z[1] - z[0])


def bore(axis: str, diameter: float, start: tuple[float, float, float], depth: float) -> Part:
    """Cylinder for cutting a hole: from `start` along +axis for `depth` (negative depth goes the other way)."""
    centre = list(start)
    centre["xyz".index(axis)] += depth / 2
    return Pos(*centre) * _AXIS_ROTATION[axis] * Cylinder(diameter / 2, abs(depth))
