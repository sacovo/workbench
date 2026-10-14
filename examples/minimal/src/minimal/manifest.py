"""Workbench manifest: what the workbench can build and show of this project.

Named in `[tool.workbench]` of pyproject.toml. Importing it builds nothing; each builder runs when
the workbench kernel asks for it, in this project's environment.
"""

from dataclasses import replace

from workbench_kernel.api import Project

from minimal import assembly
from minimal.params import BRACKET

wb = Project("minimal", units="mm", params={"minimal.params": ["PLATE", "BRACKET", "PIN"]})


@wb.assembly("minimal")
def minimal(pin_height: float = BRACKET.pin_height):
    """Plate, bracket and pin. `pin_height` (mm above the bracket's base) moves the pin and
    re-cuts the bracket's pin hole; the plate does not change."""
    return assembly.minimal_assembly(bracket_params=replace(BRACKET, pin_height=pin_height))
