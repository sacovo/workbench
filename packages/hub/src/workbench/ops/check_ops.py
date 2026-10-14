"""`wbench check`: run the project's engineering checks in the project's own environment.

The checks are registered on the project's manifest (`@wb.checks("name")`, see
workbench_kernel.checks) and run by `python -m workbench_kernel check --json` under
`uv run --project <root>`, as the kernel itself is. The CLI prints the kernel's table and exits 1
when any check fails or a group asserts nothing.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from workbench.errors import WorkbenchError
from workbench.hub.kernel import kernel_env
from workbench.ops.registry import op
from workbench.project import Project

TIMEOUT = 3600.0  # seconds; a group may run simulations


class CheckRunError(WorkbenchError):
    """The check run could not report: the environment failed, or a group name is unknown."""

    http_status = 500


def check_command(project: Project, groups: list[str]) -> list[str]:
    root = str(project.root)
    return ["uv", "run", "--project", root, "python", "-m", "workbench_kernel", "check", "--project", root, "--json", *groups]


@op(
    "check",
    summary="Run the project's engineering checks; exit 1 if any fails or asserts nothing",
    args={"groups": "check groups to run (default: all)"},
    human=lambda r: r["text"],
    exit_code=lambda r: 0 if r["ok"] else 1,
)
def check(project: Project, groups: Optional[list[str]] = None) -> dict:
    """Each check states its value, limit, unit, utilisation (value over limit, or limit over value
    for a lower limit), where the limit comes from, and whether the value is calculated, simulated
    or measured."""
    try:
        done = subprocess.run(
            check_command(project, groups or []),
            cwd=project.root,
            env=kernel_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckRunError(f"could not run the checks: {exc}") from None
    lines = done.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else None
    except ValueError:
        result = None
    if not isinstance(result, dict) or (done.returncode not in (0, 1)) or "error" in result:
        reason = result.get("error") if isinstance(result, dict) else None
        tail = done.stderr.strip()[-2000:]
        raise CheckRunError(f"check run failed (exit {done.returncode}): {reason or tail or 'no output'}")
    return result
