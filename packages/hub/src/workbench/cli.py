"""The `wb` command line entry point.

Only two things work so far: `wb --version` and `wb doctor`. Everything else
the design describes (the operation registry, `wb serve`, `wb open`, ...) comes
in later phases; see docs/plan.md. Standard library only: this runs before any
project environment is known, so it must not need a `uv sync` of its own
dependencies beyond workbench itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from workbench import __version__

# Tools the app and the agent loop need on this machine. `doctor` is a setup and
# support report, not a gate: a missing tool is reported, not an error.
TOOLS = ("uv", "chromium", "herdr", "claude", "node", "pnpm")

# Left over from an NVIDIA GPU this machine no longer has; on the real (AMD) GPU they make
# XWayland render in software or break GLX outright. See docs/plan.md, "Owner actions, optional".
STALE_GL_VARS = ("__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR")

VERSION_TIMEOUT = 2.0  # seconds: a tool that hangs or prompts on --version must not hang the doctor


def _tool_report(name: str) -> dict:
    """Whether `name` is on PATH, where, and the first line of its own --version."""
    path = shutil.which(name)
    if path is None:
        return {"found": False, "path": None, "version": None}
    version = None
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT,
        )
        lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        version = lines[0] if lines else None
    except (OSError, subprocess.SubprocessError):
        version = None  # found on PATH but would not run; still "found"
    return {"found": True, "path": path, "version": version}


def doctor() -> dict:
    """Report on the tools and environment the app and the agent loop need here."""
    virtual_env = os.environ.get("VIRTUAL_ENV")
    # `uv run` sets VIRTUAL_ENV to the environment it runs in; only a different one misdirects uv.
    foreign = bool(virtual_env) and os.path.realpath(virtual_env) != os.path.realpath(sys.prefix)
    set_gl_vars = [name for name in STALE_GL_VARS if os.environ.get(name)]

    warnings = []
    if foreign:
        warnings.append(
            f"VIRTUAL_ENV is set ({virtual_env}); uv must run with it unset, e.g. `env -u VIRTUAL_ENV uv ...`."
        )
    if set_gl_vars:
        warnings.append(f"{', '.join(set_gl_vars)} set; stale NVIDIA settings on this AMD machine break X11 GL.")

    return {
        "tools": {name: _tool_report(name) for name in TOOLS},
        "virtual_env": {"set": bool(virtual_env), "value": virtual_env, "foreign": foreign},
        "stale_gl_vars": {"checked": list(STALE_GL_VARS), "set": set_gl_vars},
        "warnings": warnings,
    }


def _print_human(report: dict) -> None:
    width = max(len(name) for name in report["tools"])
    for name, info in report["tools"].items():
        if info["found"]:
            version = info["version"] or "(no version output)"
            print(f"{name:<{width}}  found    {info['path']}  {version}")
        else:
            print(f"{name:<{width}}  missing")
    if report["warnings"]:
        print()
        for warning in report["warnings"]:
            print(f"warning: {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wb", description="Workbench: agent-driven mechanical design.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    doctor_parser = subparsers.add_parser("doctor", help="report on the tools and environment this machine needs")
    doctor_parser.add_argument("--json", action="store_true", help="print the report as JSON")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        try:
            report = doctor()
        except Exception as exc:  # the doctor's own crash is the only non-zero exit
            print(f"doctor crashed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_human(report)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
