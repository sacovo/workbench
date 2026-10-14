"""The manifest API: what a project declares to the workbench.

A project names one module in `[tool.workbench] manifest` of its pyproject.toml. That module
creates one `Project` and registers builders on it:

    wb = Project("my-robot", units="mm", params={"robot.params": ["BASE", "LIFT"]})

    @wb.assembly("robot")
    def robot(height: float | None = None):
        return assembly.robot_assembly(height)

Importing the manifest must build nothing, so the decorator only records the function and its
signature. A builder returns a build123d `Compound` whose children carry unique labels; the
kernel checks that when it builds. Every builder argument needs a default that JSON can carry
(None, bool, int, float or str), so that any assembly can be built with no arguments and the
defaults can be listed for the owner.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_JSON_SCALARS = (type(None), bool, int, float, str)


class ManifestError(Exception):
    """The manifest declares something the kernel cannot use."""


@dataclass(frozen=True)
class Argument:
    name: str
    default: Any
    annotation: str | None

    def describe(self) -> dict:
        return {"name": self.name, "default": self.default, "annotation": self.annotation}


@dataclass(frozen=True)
class AssemblySpec:
    """A registered assembly builder and the arguments it takes."""

    name: str
    builder: Callable[..., Any]
    arguments: tuple[Argument, ...]
    doc: str | None

    def describe(self) -> dict:
        return {
            "doc": self.doc,
            "builder": f"{self.builder.__module__}.{self.builder.__qualname__}",
            "args": [a.describe() for a in self.arguments],
        }

    def resolve_args(self, args: dict | None) -> dict:
        """The defaults with `args` applied on top; an argument the builder does not take is refused."""
        args = {} if args is None else args
        if not isinstance(args, dict):
            raise TypeError(f"args for assembly {self.name!r} must be an object, not {type(args).__name__}")
        known = [a.name for a in self.arguments]
        unknown = sorted(set(args) - set(known))
        if unknown:
            raise TypeError(f"assembly {self.name!r} takes no argument {', '.join(map(repr, unknown))}; it takes {known}")
        return {a.name: args.get(a.name, a.default) for a in self.arguments}


def _annotation(param: inspect.Parameter) -> str | None:
    if param.annotation is inspect.Parameter.empty:
        return None
    # A string already under `from __future__ import annotations`; otherwise the type's own text.
    return param.annotation if isinstance(param.annotation, str) else inspect.formatannotation(param.annotation)


def _arguments(name: str, fn: Callable[..., Any]) -> tuple[Argument, ...]:
    out = []
    for param in inspect.signature(fn).parameters.values():
        where = f"builder {fn.__qualname__!r} of assembly {name!r}, argument {param.name!r}"
        if param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise ManifestError(f"{where}: only named arguments with defaults are allowed, not {param.kind.description}")
        if param.default is inspect.Parameter.empty:
            raise ManifestError(f"{where}: needs a default, so the assembly can be built without arguments")
        if not isinstance(param.default, _JSON_SCALARS):
            raise ManifestError(
                f"{where}: default {param.default!r} is a {type(param.default).__name__}; "
                "use None, bool, int, float or str and build the object inside the builder"
            )
        out.append(Argument(param.name, param.default, _annotation(param)))
    return tuple(out)


class Project:
    """A project's declaration: its name, units, parameter objects and registered builders."""

    def __init__(self, name: str, units: str = "mm", params: dict[str, list[str]] | None = None):
        if not isinstance(name, str) or not name:
            raise ManifestError("Project needs a non-empty name")
        if not isinstance(units, str) or not units:
            raise ManifestError("Project units must be a non-empty string such as 'mm'")
        params = {} if params is None else params
        if not isinstance(params, dict) or not all(
            isinstance(module, str) and isinstance(names, (list, tuple)) and all(isinstance(n, str) for n in names)
            for module, names in params.items()
        ):
            raise ManifestError("Project params must map a module name to a list of object names")
        self.name = name
        self.units = units
        self.params = {module: list(names) for module, names in params.items()}
        self.assemblies: dict[str, AssemblySpec] = {}

    def assembly(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register an assembly builder under `name`. The function is returned unchanged and not called."""
        if not isinstance(name, str) or not name:
            raise ManifestError("an assembly needs a non-empty name")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self.assemblies:
                raise ManifestError(f"assembly {name!r} is registered twice")
            doc = inspect.getdoc(fn)
            self.assemblies[name] = AssemblySpec(name, fn, _arguments(name, fn), doc)
            return fn

        return register

    def check_params(self) -> None:
        """Every declared parameter object exists. Imports the parameter modules, so the kernel
        calls it only after the manifest itself has been imported."""
        import importlib

        missing = []
        for module_name, names in self.params.items():
            module = importlib.import_module(module_name)
            missing += [f"{module_name}.{n}" for n in names if not hasattr(module, n)]
        if missing:
            raise ManifestError(f"declared parameters do not exist: {', '.join(missing)}")

    def describe(self) -> dict:
        return {
            "name": self.name,
            "units": self.units,
            "assemblies": {name: spec.describe() for name, spec in self.assemblies.items()},
            "params": {module: list(names) for module, names in self.params.items()},
        }
