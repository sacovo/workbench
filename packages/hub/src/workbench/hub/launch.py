"""The owner's window: `chromium --app` on native Wayland, pointed at a running hub.

The URL carries the hub's token in its fragment (`#token=...`): a fragment is never sent to the
server, so it stays out of request lines and logs; the page keeps it in sessionStorage so a reload
works. The window gets its own profile under `.workbench/chromium`, so it starts its own browser
process with these flags instead of handing the URL to an already running Chromium (which would
ignore them), and it never touches the owner's browsing profile.

The stale NVIDIA variables from `~/.zprofile` are removed from the window's environment: with them
set, GL on this AMD machine falls back to software (docs/plan.md, owner actions).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from workbench.errors import WorkbenchError
from workbench.ops.doctor import STALE_GL_VARS
from workbench.project import HubInfo, Project

BROWSER = "chromium"


def window_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in STALE_GL_VARS and k != "VIRTUAL_ENV"}


def page_url(hub: HubInfo) -> str:
    return f"{hub.url}/#token={hub.token}"


def window_command(executable: str, url: str, profile: str) -> list[str]:
    return [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--ozone-platform=wayland",  # native Wayland, not XWayland
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
    ]


def open_window(project: Project, hub: HubInfo) -> dict:
    """Start the app window detached; it outlives this process."""
    executable = shutil.which(BROWSER)
    if executable is None:
        raise WorkbenchError(f"{BROWSER} is not on PATH")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise WorkbenchError("no Wayland session here (WAYLAND_DISPLAY is not set); run `wb open` from the desktop session")
    profile = project.ensure_state_dir() / "chromium"
    log = project.ensure_logs_dir() / "chromium.log"
    command = window_command(executable, page_url(hub), str(profile))
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            command,
            env=window_env(),
            cwd=project.root,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "url": f"{hub.url}/",
        "pid": proc.pid,
        "profile": str(profile),
        "log": str(log),
        "command": [c if not c.startswith("--app=") else f"--app={hub.url}/#token=..." for c in command],
    }
