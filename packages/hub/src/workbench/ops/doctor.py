"""`wbench doctor`: a setup and support report, not a gate. A missing tool is reported, not an error."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from workbench.ops.registry import op

# Tools the app and the agent loop need on this machine.
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


def _human(report: dict) -> str:
    width = max(len(name) for name in report["tools"])
    lines = []
    for name, info in report["tools"].items():
        if info["found"]:
            version = info["version"] or "(no version output)"
            lines.append(f"{name:<{width}}  found    {info['path']}  {version}")
        else:
            lines.append(f"{name:<{width}}  missing")
    if report["warnings"]:
        lines.append("")
        lines += [f"warning: {warning}" for warning in report["warnings"]]
    return "\n".join(lines)


@op("doctor", summary="Report on the tools and environment this machine needs", human=_human)
def doctor() -> dict:
    """Which of the tools the app and the agent loop need are on PATH, and environment warnings."""
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
