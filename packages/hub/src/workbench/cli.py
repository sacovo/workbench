"""The `wb` command line, generated from the operation registry (workbench.ops).

Every operation is a subcommand. Required text and number arguments are positional, everything
else is an option. List and object arguments take JSON (`--questions '[...]'`). `--file args.json`
(or `-` for stdin) supplies any arguments as one JSON object, the same object the hub's HTTP
endpoint takes; options on the command line win over the file. Output is short and
human-readable; `--json` prints the raw result. Errors go to stderr, prefixed with the command,
and the exit code is 1 (2 for a malformed command line).

Operations registered with needs_hub=True are forwarded to the running hub over HTTP (address and
token from .workbench/hub.json); all others run here, in this process. Operations registered with
owner_only=True (answer, comment: their author role is fixed to the owner) are refused here when
CLAUDECODE is set, so a Claude Code agent's shell cannot run them; a real owner action comes from
the workbench page, which calls the same operation directly, never through this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from workbench import __version__
from workbench.errors import InvalidInput, WorkbenchError
from workbench.ops import Op, Param, Registry, default_registry
from workbench.ops.client import forward
from workbench.ops.doctor import STALE_GL_VARS, TOOLS, doctor  # noqa: F401  (kept importable from here)
from workbench.project import find_project

POSITIONAL_TYPES = ("str", "int", "float", "literal")
JSON_TYPES = ("dict", "list[str]", "list[dict]")


class OwnerOnly(WorkbenchError):
    """An operation whose author role is fixed to the owner (registered `owner_only=True`),
    refused here when running as a Claude Code agent's shell. Never raised on the hub's HTTP
    path: the UI, acting for the owner, calls the same operation directly, not through this
    module's `execute`."""

    http_status = 403


def _json_value(text: str):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from None


def _add_param(parser: argparse.ArgumentParser, p: Param) -> None:
    help_text = p.help or None
    if p.required and p.type in POSITIONAL_TYPES:
        # Optional to argparse so that --file can supply it; required-ness is checked after merging.
        kwargs: dict = {"nargs": "?", "default": argparse.SUPPRESS, "help": help_text}
        if p.type == "literal":
            kwargs["choices"] = p.choices
        else:
            kwargs["metavar"] = p.name.upper()
            if p.type == "int":
                kwargs["type"] = int
            elif p.type == "float":
                kwargs["type"] = float
        parser.add_argument(p.name, **kwargs)
        return
    flag = "--" + p.name.replace("_", "-")
    kwargs = {"dest": p.name, "default": argparse.SUPPRESS, "help": help_text}
    if p.type == "bool":
        parser.add_argument(flag, action=argparse.BooleanOptionalAction, **kwargs)
    elif p.type in JSON_TYPES:
        parser.add_argument(flag, type=_json_value, metavar="JSON", **kwargs)
    elif p.type == "literal":
        parser.add_argument(flag, choices=p.choices, **kwargs)
    elif p.type == "int":
        parser.add_argument(flag, type=int, metavar="N", **kwargs)
    elif p.type == "float":
        parser.add_argument(flag, type=float, metavar="X", **kwargs)
    else:
        parser.add_argument(flag, metavar="TEXT", **kwargs)


def build_parser(registry: Registry) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wb", description="Workbench: agent-driven mechanical design.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-C", dest="_directory", metavar="DIR", help="find the project from DIR instead of the current directory"
    )
    sub = parser.add_subparsers(dest="_command", metavar="COMMAND")
    for o in registry:
        description = o.summary + (f"\n\n{o.description}" if o.description else "")
        if o.needs_hub:
            description += "\n\nRuns in the hub (`wb serve`)."
        sp = sub.add_parser(
            o.name, help=o.summary, description=description, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        for p in o.params:
            _add_param(sp, p)
        sp.add_argument(
            "--file", metavar="PATH", help="JSON object with arguments ('-' reads stdin); command-line options win"
        )
        sp.add_argument("--json", action="store_true", help="print the raw JSON result")
        sp.set_defaults(_op=o)
    return parser


def _read_arguments_file(path: str) -> dict:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidInput(f"--file {path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InvalidInput(f"--file {path}: not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InvalidInput(f"--file {path}: expected a JSON object of arguments, got {type(data).__name__}")
    return data


def _arguments(o: Op, args: argparse.Namespace) -> dict:
    merged = _read_arguments_file(args.file) if args.file is not None else {}
    for p in o.params:
        if hasattr(args, p.name):
            merged[p.name] = getattr(args, p.name)
    return merged


def _with_session_default(o: Op, arguments: dict) -> dict:
    """`session` falls back to $WB_SESSION (the plugin's SessionStart hook exports it, see
    plugin/hooks/wb_hook.py write_env_file) for any operation that takes one, when the caller gave
    no `session` key at all (an explicit null still counts as given). Used by execute(), so both
    `wb` and the MCP shim (which calls execute() too) pick it up without naming the session
    themselves on every call."""
    if "session" in arguments or not any(p.name == "session" for p in o.params):
        return arguments
    session = os.environ.get("WB_SESSION")
    return {**arguments, "session": session} if session else arguments


def execute(o: Op, arguments: dict, directory: str | None = None) -> dict:
    """Run one operation as `wb` does: in the hub if it needs one, else here.

    `directory` is where the project is looked for (default: the current directory)."""
    if o.owner_only and os.environ.get("CLAUDECODE"):
        raise OwnerOnly("owner actions come from the workbench page, not the agent's shell")
    arguments = _with_session_default(o, arguments)
    if o.needs_hub:
        o.bind(arguments)  # reject bad arguments here, before the round trip
        return forward(find_project(directory), o.name, arguments)
    project = find_project(directory) if o.needs_project else None
    return o.call(arguments, project=project)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(default_registry())
    args = parser.parse_args(argv)
    o: Op | None = getattr(args, "_op", None)
    if o is None:
        parser.print_help()
        return 0
    try:
        result = execute(o, _arguments(o, args), args._directory)
    except WorkbenchError as exc:
        print(f"wb {o.name}: {exc}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        print(f"wb {o.name}: internal error (a bug; traceback above)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(o.render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
