"""One registry of operations. The `wb` subcommands, the hub's HTTP endpoints and the MCP tools are
all generated from it, so they cannot drift (docs/plan.md, design principle 2).

An operation is a function with type-annotated parameters that returns a JSON-serialisable dict:

    @op("reply", summary="Reply to an item", args={"id": "item id, e.g. R-0001"})
    def reply(project: Project, id: str, body: str, status: Optional[Status] = None) -> dict:

Parameter types: str, int, float, bool, list[str], list[dict], dict, Optional[X] of these, and
Literal of strings. A parameter annotated `Project` is not an argument: the caller injects it (the
CLI finds the project from the current directory, the hub knows its own).

The same rules hold for every caller:
- arguments are one JSON object; unknown keys, missing required ones and wrong types are errors
  (no "true" for a boolean, no 1 for a boolean, no true for an integer; an int is accepted for a
  float);
- the result is a JSON object;
- errors are WorkbenchError subclasses carrying a message and an HTTP status.

HTTP contract for the hub: POST /api/op/<name>, the arguments object as the JSON body, header
`X-Workbench-Token`; the answer is 200 with the result object, or the error's status with
`{"error": message}`. `Registry.http_endpoints()` describes the routes; no web framework here.
"""

from __future__ import annotations

import copy
import inspect
import json
import keyword
import math
import re
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Union, get_args, get_origin, get_type_hints

from workbench.errors import InvalidInput, ProjectError
from workbench.project import Project

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# CLI options every subcommand has; no operation parameter may take these names.
RESERVED = frozenset({"json", "file", "help", "h"})
# Types the caller provides instead of the arguments.
INJECTABLE: tuple[type, ...] = (Project,)

_SCALAR_SCHEMAS = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "dict": {"type": "object"},
    "list[str]": {"type": "array", "items": {"type": "string"}},
    "list[dict]": {"type": "array", "items": {"type": "object"}},
}


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # str | int | float | bool | dict | list[str] | list[dict] | literal
    choices: tuple[str, ...] = ()
    nullable: bool = False
    required: bool = True
    default: Any = None
    help: str = ""

    def schema(self) -> dict:
        base = {"type": "string", "enum": list(self.choices)} if self.type == "literal" else dict(_SCALAR_SCHEMAS[self.type])
        out: dict = {"anyOf": [base, {"type": "null"}]} if self.nullable else base
        if self.help:
            out["description"] = self.help
        if not self.required and self.default is not None:
            out["default"] = self.default
        return out

    def check(self, value: Any, where: str) -> Any:
        """Return `value` if it has this parameter's type; raise InvalidInput otherwise."""
        if value is None:
            if self.nullable:
                return None
            raise InvalidInput(f"{where}: must not be null")
        t = self.type
        ok = (
            (t == "str" and isinstance(value, str))
            or (t == "int" and type(value) is int)
            or (t == "float" and not isinstance(value, bool) and isinstance(value, (int, float)))
            or (t == "bool" and type(value) is bool)
            or (t == "dict" and isinstance(value, dict))
            or (t == "list[str]" and isinstance(value, list) and all(isinstance(v, str) for v in value))
            or (t == "list[dict]" and isinstance(value, list) and all(isinstance(v, dict) for v in value))
            or (t == "literal" and isinstance(value, str) and value in self.choices)
        )
        if not ok:
            expected = f"one of {', '.join(self.choices)}" if t == "literal" else _expected(t)
            raise InvalidInput(f"{where}: expected {expected}, got {_describe(value)}")
        if t == "float":
            if not math.isfinite(value):
                raise InvalidInput(f"{where}: must be finite")
            return float(value)
        return value


def _expected(t: str) -> str:
    return {
        "str": "a string",
        "int": "an integer",
        "float": "a number",
        "bool": "true or false",
        "dict": "a JSON object",
        "list[str]": "a list of strings",
        "list[dict]": "a list of JSON objects",
    }[t]


def _describe(value: Any) -> str:
    kind = {bool: "boolean", int: "integer", float: "number", str: "string", list: "list", dict: "object"}.get(
        type(value), type(value).__name__
    )
    text = json.dumps(value) if isinstance(value, (bool, int, float, str)) else ""
    if len(text) > 40:
        text = text[:37] + "..."
    return f"{kind} {text}".strip()


@dataclass
class Op:
    name: str
    fn: Callable[..., dict]
    summary: str
    needs_hub: bool
    params: tuple[Param, ...]
    injected: tuple[tuple[str, type], ...]
    human: Callable[[dict], str] | None = None
    description: str = ""
    # Author role fixed to the owner (answer, comment: see workbench.ops.review_ops): the CLI
    # refuses to run it as a Claude Code agent's shell (workbench.cli.execute). Never checked on
    # the hub's HTTP path, which calls Op.call() directly.
    owner_only: bool = False

    @property
    def needs_project(self) -> bool:
        return any(t is Project for _, t in self.injected) or self.needs_hub

    def schema(self) -> dict:
        """JSON schema of the arguments object (for HTTP bodies and MCP tool inputs)."""
        return {
            "type": "object",
            "properties": {p.name: p.schema() for p in self.params},
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def bind(self, arguments: Any) -> dict:
        """Validate an arguments object strictly; return keyword arguments with defaults filled in."""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise InvalidInput(f"arguments must be a JSON object, got {_describe(arguments)}")
        known = {p.name for p in self.params}
        unknown = sorted(k for k in arguments if k not in known)
        if unknown:
            raise InvalidInput(
                f"unknown argument(s) {', '.join(map(str, unknown))}; "
                f"known: {', '.join(p.name for p in self.params) or 'none'}"
            )
        missing = [p.name for p in self.params if p.required and p.name not in arguments]
        if missing:
            raise InvalidInput(f"missing argument(s) {', '.join(missing)}")
        kwargs = {}
        for p in self.params:
            if p.name in arguments:
                kwargs[p.name] = p.check(arguments[p.name], f"argument {p.name}")
            else:
                kwargs[p.name] = copy.deepcopy(p.default)
        return kwargs

    def call(self, arguments: Any = None, *, project: Project | None = None) -> dict:
        """Run the operation here, in this process, with `arguments` as the JSON object."""
        kwargs = self.bind(arguments)
        for name, t in self.injected:
            if t is Project:
                if project is None:
                    raise ProjectError(f"{self.name} needs a project")
                kwargs[name] = project
        result = self.fn(**kwargs)
        if not isinstance(result, dict):
            raise TypeError(f"operation {self.name} returned {type(result).__name__}, not a dict")
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"operation {self.name} returned a result that is not JSON: {exc}") from None
        return result

    def render(self, result: dict) -> str:
        if self.human is not None:
            return self.human(result)
        return json.dumps(result, indent=2, ensure_ascii=False)


@dataclass(frozen=True)
class Endpoint:
    """One HTTP route for the hub to mount: POST `path`, JSON body in, JSON object out."""

    name: str
    method: str
    path: str
    summary: str
    needs_hub: bool
    schema: dict
    call: Callable[[Any], dict] = field(repr=False)


@dataclass
class Registry:
    ops: dict[str, Op] = field(default_factory=dict)

    def op(
        self,
        name: str,
        *,
        summary: str,
        needs_hub: bool = False,
        owner_only: bool = False,
        args: dict[str, str] | None = None,
        human: Callable[[dict], str] | None = None,
    ) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
        """Decorator registering an operation; see the module docstring."""

        def register(fn: Callable[..., dict]) -> Callable[..., dict]:
            if not NAME_RE.match(name):
                raise TypeError(f"operation name {name!r}: lowercase words joined by '-'")
            if name in self.ops:
                raise TypeError(f"operation {name!r} is registered twice")
            params, injected = _introspect(name, fn, args or {})
            self.ops[name] = Op(
                name=name,
                fn=fn,
                summary=summary,
                needs_hub=needs_hub,
                params=params,
                injected=injected,
                human=human,
                description=inspect.getdoc(fn) or "",
                owner_only=owner_only,
            )
            return fn

        return register

    def __iter__(self):
        return iter(self.ops.values())

    def __len__(self) -> int:
        return len(self.ops)

    def get(self, name: str) -> Op:
        try:
            return self.ops[name]
        except KeyError:
            raise InvalidInput(f"no operation {name!r}") from None

    def http_endpoints(self, project: Project | None = None) -> list[Endpoint]:
        """The hub's routes, each calling the operation in-process with `project` injected."""
        return [
            Endpoint(
                name=o.name,
                method="POST",
                path=f"/api/op/{o.name}",
                summary=o.summary,
                needs_hub=o.needs_hub,
                schema=o.schema(),
                call=_bound_call(o, project),
            )
            for o in self
        ]

    def mcp_tools(self) -> list[dict]:
        """MCP tool definitions: name, description, inputSchema (the same schemas as HTTP)."""
        return [
            {
                "name": o.name,
                "description": o.summary + (f"\n\n{o.description}" if o.description else ""),
                "inputSchema": o.schema(),
            }
            for o in self
        ]


def _bound_call(o: Op, project: Project | None) -> Callable[[Any], dict]:
    def call(body: Any = None) -> dict:
        return o.call(body, project=project)

    return call


def _introspect(name: str, fn: Callable, helps: dict[str, str]) -> tuple[tuple[Param, ...], tuple[tuple[str, type], ...]]:
    hints = get_type_hints(fn)
    signature = inspect.signature(fn)
    params: list[Param] = []
    injected: list[tuple[str, type]] = []
    for pname, p in signature.parameters.items():
        if p.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise TypeError(f"{name}: parameter {pname} must be a plain or keyword-only parameter")
        if pname not in hints:
            raise TypeError(f"{name}: parameter {pname} needs a type annotation")
        annotation = hints[pname]
        if annotation in INJECTABLE:
            injected.append((pname, annotation))
            continue
        if pname in RESERVED or keyword.iskeyword(pname) or pname.startswith("_"):
            raise TypeError(f"{name}: parameter name {pname!r} is reserved")
        ptype, choices, nullable = _classify(name, pname, annotation)
        required = p.default is inspect.Parameter.empty
        default = None if required else p.default
        if not required and default is not None:
            Param(pname, ptype, choices, nullable).check(default, f"{name}: default of {pname}")
        params.append(
            Param(
                name=pname,
                type=ptype,
                choices=choices,
                nullable=nullable,
                required=required,
                default=default,
                help=helps.get(pname, ""),
            )
        )
    stray = sorted(set(helps) - {p.name for p in params})
    if stray:
        raise TypeError(f"{name}: help given for unknown parameter(s) {', '.join(stray)}")
    return tuple(params), tuple(injected)


def _classify(name: str, pname: str, annotation: Any) -> tuple[str, tuple[str, ...], bool]:
    nullable = False
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = get_args(annotation)
        rest = [a for a in members if a is not type(None)]
        if len(members) != 2 or len(rest) != 1:
            raise TypeError(f"{name}: parameter {pname}: only Optional[X] unions are supported")
        annotation, nullable = rest[0], True
        origin = get_origin(annotation)
    if annotation is bool:
        return "bool", (), nullable
    if annotation is str:
        return "str", (), nullable
    if annotation is int:
        return "int", (), nullable
    if annotation is float:
        return "float", (), nullable
    if annotation is dict or origin is dict:
        return "dict", (), nullable
    if origin is list:
        (item,) = get_args(annotation) or (None,)
        if item is str:
            return "list[str]", (), nullable
        if item is dict or get_origin(item) is dict:
            return "list[dict]", (), nullable
    if origin is Literal:
        choices = get_args(annotation)
        if choices and all(isinstance(c, str) for c in choices):
            return "literal", tuple(choices), nullable
    raise TypeError(f"{name}: parameter {pname}: unsupported type {annotation!r}")


REGISTRY = Registry()
op = REGISTRY.op
