"""Operations about the hub and the page: `serve`, `open`, `show`, `selection` and `agents`.

`serve` runs in the calling process (it starts the hub). `open`, `show`, `selection` and `agents`
need the hub: the CLI forwards them over HTTP and they run in the hub process, where the kernel,
the page, the agent link and (for `open`) the page credential are. `open` opens the owner's window
from there precisely so that the page credential, which only the hub process holds in memory, never
has to pass through the CLI or appear in this operation's result (workbench.hub.app, workbench.hub.
launch). The hub's modules are imported inside the functions, so that every other `wbench` command
stays free of the web stack's import time.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from workbench.errors import HubNotRunning
from workbench.ops.registry import op
from workbench.project import Project

SHOW_TIMEOUT = 1000.0  # s: a new kernel's start plus a build, both bounded by the hub's own deadlines


def _human_serve(r: dict) -> str:
    return f"hub on {r['url']} stopped"


def _human_open(r: dict) -> str:
    return f"opened {r['url']} (chromium pid {r['pid']}, profile {r['profile']})"


def _human_show(r: dict) -> str:
    args = json.dumps(r["args"], ensure_ascii=False)
    if r["incremental"]:
        how = f"incremental: {len(r['moved'])} moved, {len(r['changed'])} changed"
    else:
        how = "full" + (f" ({r['reason']})" if r.get("reason") else "")
    kernel = f"new kernel {r['generation']}" if r["new_kernel"] else f"kernel {r['generation']}"
    lines = [
        f"{r['assembly']} {args}: scene {r['scene']}, {len(r['parts'])} parts, build {r['build']}",
        f"  {how}; {kernel}; {r['message_bytes']} bytes to {r['pages']} page(s) in {r['timings']['hub_total_s']:.2f} s",
    ]
    if r["moved"]:
        lines.append(f"  moved: {', '.join(r['moved'])}")
    if r["changed"]:
        lines.append(f"  changed: {', '.join(r['changed'])}")
    return "\n".join(lines)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_fmt(x)}" for k, x in v.items()) + "}"
    return str(v)


def _human_selection(r: dict) -> str:
    head = f"{r['label']} ({r['kind']}" + (f", {r['geom_type']}" if r.get("geom_type") else "") + f") in {r['assembly']}"
    lines = [head + ("" if r["current"] else "  [picked on an earlier scene]")]
    for key, value in r["world"].items():
        lines.append(f"  {key}: {_fmt(value)}")
    if r.get("point"):
        lines.append(f"  picked point: {_fmt(r['point'])}")
    if r.get("camera"):
        lines.append(f"  camera: {_fmt({k: r['camera'][k] for k in ('position', 'target', 'zoom') if k in r['camera']})}")
    lines.append(f"  scene {r['scene']}, build {r['build']}, {r['via']}, {r['at']}")
    return "\n".join(lines)


def _human_agents(r: dict) -> str:
    checked = f", agents checked {r['agents_checked_at']}" if r["agents_checked_at"] else ", agents not checked yet"
    lines = [f"transport: {r['transport']}{checked}"]
    if r["sessions"]:
        for s in r["sessions"]:
            agent = s.get("agent")
            where = s["pane"] or "no pane"
            status = agent["status"] if agent else "no agent seen there"
            lines.append(f"  {s['session']} ({where}, {status}): turn {s['turn']}, {s['turns']} prompt(s), last seen {s['last_seen']}")
    else:
        lines.append("  no sessions registered")
    if r["waiting"]:
        lines.append("waiting for the agent:")
        for w in r["waiting"]:
            lines.append(f"  {w['id']} {w['kind']} \"{w['title']}\" ({w['status']})" + ("" if w["due"] else " [not due]"))
    else:
        lines.append("waiting for the agent: none")
    return "\n".join(lines)


@op(
    "serve",
    summary="Run the hub for this project in the foreground: the page, its API and the kernel",
    human=_human_serve,
    args={"port": "TCP port on 127.0.0.1 (default 0: a free one)", "open": "also open the page in a Chromium app window"},
)
def serve(project: Project, port: int = 0, open: bool = False) -> dict:
    """Binds 127.0.0.1 only, writes the port and a fresh token to .workbench/hub.json, starts the
    project's kernel (`uv run --project <root> python -m workbench_kernel serve`) and runs until
    Ctrl-C or SIGTERM. `wbench open` opens the page."""
    from workbench.hub.server import run

    return run(project, port=port, open_window_too=open)


@op("open", summary="Open the running hub's page in a Chromium app window (native Wayland)", needs_hub=True, human=_human_open)
def open_(project: Project) -> dict:
    """Forwarded over HTTP to the running hub like show, selection and agents, and runs there:
    the hub process is the only place the page credential exists (never in .workbench/hub.json),
    so opening the window from inside it, rather than from the CLI's own process, is what keeps
    the credential out of the CLI entirely. Starts `chromium --app` with its own profile in
    .workbench/chromium, the stale NVIDIA variables removed and native Wayland; the result below
    never contains the page credential (workbench.hub.launch redacts it from the URL it returns)."""
    from workbench.hub.hub import running_instance
    from workbench.hub.launch import open_window
    from workbench.project import HubInfo

    hub = running_instance(project)
    info = HubInfo(pid=os.getpid(), port=hub.port, token=hub.token)
    return open_window(project, info, hub.pairing_code())


@op(
    "show",
    summary="Build an assembly in the kernel and show it in the page",
    needs_hub=True,
    human=_human_show,
    args={
        "assembly": "an assembly of the manifest, e.g. robot",
        "args": "JSON object of builder arguments, e.g. '{\"height\": 500}'",
        "rebuild": "build in a new kernel even if the code did not change",
    },
)
def show(project: Project, assembly: str, args: Optional[dict] = None, rebuild: bool = False) -> dict:
    """Builds in the running kernel when the code is unchanged: the same assembly is updated
    in place and only moved and changed parts reach the page. A code change (or --rebuild) builds
    in a new kernel while the old one keeps answering picks, then swaps. Returns the scene summary."""
    from workbench.hub.hub import running_instance

    hub = running_instance(project)
    return hub.call(hub.show(assembly, args or {}, rebuild=rebuild), timeout=SHOW_TIMEOUT)


@op("selection", summary="The owner's current pick in the page, resolved by the kernel", needs_hub=True, human=_human_selection)
def selection(project: Project) -> dict:
    """The part label, what was picked (face, edge, vertex or the whole part), its exact properties
    in world and part coordinates, the picked point and the camera. An error if nothing is picked."""
    from workbench.hub.hub import PICK_TIMEOUT, running_instance

    hub = running_instance(project)
    return hub.call(hub.get_selection(), timeout=PICK_TIMEOUT + 10)


@op("agents", summary="Agent sessions, herdr state and what waits for them", needs_hub=True, human=_human_agents)
def agents(project: Project) -> dict:
    """The same information as GET /api/agents: every Claude Code session the agent link knows of
    from its hooks, herdr's state for it at the last look, what waits for the agent, and the
    link's latest wake attempts and owner notices. From memory only; never touches herdr or disk."""
    from workbench.hub.hub import running_instance

    hub = running_instance(project)
    if hub.link is None:
        raise HubNotRunning("this hub has no agent link (only `wbench serve` wires one)")
    return hub.link.snapshot()
