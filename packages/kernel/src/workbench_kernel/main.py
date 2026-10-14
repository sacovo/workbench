"""`python -m workbench_kernel serve --project DIR`: the kernel's protocol, JSON lines on stdio.

Request:  {"id": <any>, "op": "<op>", "args": {...}}
Response: {"id": <same>, "ok": true, "result": {...}}
          {"id": <same>, "ok": false, "error": {"type": "...", "message": "...", "traceback": "..."}}

Ops (see `workbench_kernel.ops`): hello, build, update, resolve, backend, facts. Requests are
answered one at a time, in order. A request that fails gets an error response and the kernel
goes on; a line that is not a JSON object gets one with id null. End of stdin ends the kernel.

stdout carries protocol lines and nothing else. Before anything heavy is imported, the real
stdout is moved to a private file descriptor and fd 1 is pointed at stderr, so whatever a
library prints, from Python or from C, lands on stderr. stdin is moved the same way and fd 0
reads /dev/null, so nothing a builder calls can consume a request.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
import traceback
from pathlib import Path

OPS = ("hello", "build", "update", "resolve", "backend", "facts")


class _Stdio:
    """The protocol's private ends of stdin and stdout; fd 0 and fd 1 are left to everything else."""

    def __init__(self):
        sys.stdout.flush()
        self._out = os.fdopen(os.dup(1), "wb")
        self._in = os.fdopen(os.dup(0), "rb")
        os.dup2(2, 1)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        sys.stdout = sys.stderr

    def requests(self):
        while line := self._in.readline():
            if line.strip():
                yield line

    def send(self, line: bytes) -> None:
        self._out.write(line + b"\n")
        self._out.flush()


def _error(exc: BaseException) -> dict:
    return {"type": type(exc).__name__, "message": str(exc), "traceback": "".join(traceback.format_exception(exc))}


def _fallback_encode(obj) -> bytes:
    return json.dumps(obj, default=repr).encode()


def handle(kernel, raw: bytes, encode=_fallback_encode, startup_error: BaseException | None = None) -> bytes:
    """One request line to one response line."""
    rid = None
    try:
        try:
            request = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"a request is one JSON object per line: {exc}") from None
        if not isinstance(request, dict):
            raise ValueError("a request is a JSON object with id, op and args")
        rid = request.get("id")
        op, args = request.get("op"), request.get("args")
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}; the kernel answers {list(OPS)}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError(f"args of {op} must be an object")
        if kernel is None:
            raise RuntimeError(f"the kernel failed to start: {startup_error!r}") from startup_error
        method = getattr(kernel, f"op_{op}")
        try:
            bound = inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise TypeError(f"{op}: {exc}") from None
        return encode({"id": rid, "ok": True, "result": method(*bound.args, **bound.kwargs)})
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - every failure is an answer, not an exit
        try:
            return encode({"id": rid, "ok": False, "error": _error(exc)})
        except Exception:  # noqa: BLE001 - an id that cannot be encoded
            return _fallback_encode({"id": None, "ok": False, "error": _error(exc)})


def serve(project: Path) -> int:
    stdio = _Stdio()
    t = time.perf_counter()
    kernel, encode, startup_error = None, _fallback_encode, None
    try:
        from workbench_kernel.ops import Kernel, encode  # heavy: OCP, ocp-tessellate, ocp-viewer-core

        imports_s = time.perf_counter() - t
        kernel = Kernel(project, imports_s=imports_s)
    except Exception as exc:  # noqa: BLE001 - answered on every request instead
        startup_error = exc
        traceback.print_exc()
    for line in stdio.requests():
        stdio.send(handle(kernel, line, encode, startup_error))
    return 0


def main(argv: list[str] | None = None) -> int:
    from workbench_kernel import __version__

    parser = argparse.ArgumentParser(prog="python -m workbench_kernel", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="answer the protocol on stdin and stdout")
    serve_parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root (default: current directory)")
    check_parser = sub.add_parser("check", help="run the project's check groups and print the result")
    check_parser.add_argument("groups", nargs="*", help="groups to run (default: all)")
    check_parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root (default: current directory)")
    check_parser.add_argument("--json", action="store_true", help="print JSON (with the table as 'text')")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(args.project, args.groups, args.json)
    return serve(args.project)


def check(project_root: Path, groups: list[str], as_json: bool) -> int:
    """Run check groups. Exit 0 when every check passes, 1 when any fails or a group asserts
    nothing, 2 when the project or a group name is wrong. With --json, stdout carries exactly one
    JSON object and whatever the checks print goes to stderr."""
    import contextlib
    import importlib

    from workbench_kernel.api import Project
    from workbench_kernel.checks import CheckError, run_checks, table
    from workbench_kernel.paths import manifest_module

    out = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        try:
            module = importlib.import_module(manifest_module(project_root.resolve()))
            projects = [v for v in vars(module).values() if isinstance(v, Project)]
            if len(projects) != 1:
                raise CheckError(f"manifest {module.__name__} must create exactly one Project, has {len(projects)}")
            result = run_checks(projects[0], groups or None)
        except Exception as exc:  # noqa: BLE001 - a usage error, reported as such
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if "error" in result:
        out.write(json.dumps(result) + "\n" if as_json else f"check: {result['error']}\n")
        return 2
    result["text"] = table(result)
    out.write(json.dumps(result, allow_nan=False) + "\n" if as_json else result["text"] + "\n")
    return 0 if result["ok"] else 1
