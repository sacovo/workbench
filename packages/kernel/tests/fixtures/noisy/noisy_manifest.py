"""A manifest that does what real project code may do to a kernel's stdio, and worse.

It prints through Python and writes to file descriptor 1 directly (as C libraries do), at import
and in a builder; a builder reads stdin; one builder fails; one assembly has a root with a
location of its own; one changes its set of parts with an argument.
"""

import os
import sys

from build123d import Box, Compound, Location, Pos

from workbench_kernel.api import Project

print("noise from print at import")
os.write(1, b"noise on fd 1 at import\n")

wb = Project("noisy", units="mm")


def _boxes(size: float, extra: bool) -> list:
    a = Box(size, size, size)
    a.label = "a"
    b = Pos(3 * size, 0, 0) * Box(size, size, size)
    b.label = "b"
    children = [a, b]
    if extra:
        c = Pos(0, 3 * size, 0) * Box(size, size, size)
        c.label = "c"
        children.append(c)
    return children


@wb.assembly("boxes")
def boxes(size: float = 10.0, extra: bool = False):
    print("noise from print in a builder")
    os.write(1, b"noise on fd 1 in a builder\n")
    if sys.stdin.read():  # with stdin left to the protocol this would block, or eat a request
        raise RuntimeError("a builder read protocol input")
    return Compound(label="boxes", children=_boxes(size, extra))


@wb.assembly("located")
def located(x: float = 100.0):
    """The same boxes under a root placed at (x, 0, 0)."""
    root = Compound(label="located", children=_boxes(10.0, False))
    root.location = Location((x, 0, 0))
    return root


@wb.assembly("broken")
def broken():
    print("noise from a builder about to fail")
    raise RuntimeError("deliberate failure in a builder")
