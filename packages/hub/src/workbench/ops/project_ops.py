"""Operations about the project as a whole: `status` and `snapshot`."""

from __future__ import annotations

from workbench.buildid import build_state
from workbench.errors import GitError
from workbench.ops.registry import op
from workbench.project import Project, running_hub
from workbench.review import STATUSES, ReviewStore
from workbench.snapshot import snapshot


def _manifest_file(project: Project) -> str | None:
    """Where the manifest module's source is, if it is in a src/ or flat layout of this project."""
    parts = project.manifest.split(".")
    for base in ("src", "."):
        for candidate in (f"{'/'.join(parts)}.py", f"{'/'.join(parts)}/__init__.py"):
            path = project.root / base / candidate
            if path.is_file():
                return path.relative_to(project.root).as_posix()
    return None


def _human_status(r: dict) -> str:
    build = r["build"]
    if build["id"] is None:
        build_line = f"none ({build['error']})"
    elif build["dirty"]:
        build_line = f"{build['id']} (dirty: {build['changed']} changed, {build['untracked']} untracked)"
    else:
        build_line = f"{build['id']} (clean)"
    hub = r["hub"]
    hub_line = f"running, pid {hub['pid']}, {hub['url']}" if hub["running"] else f"not running ({hub['reason']})"
    items = r["items"]
    manifest = r["manifest"]
    lines = [
        f"project   {r['project']['name']}  {r['project']['root']}",
        f"manifest  {manifest['module']}  ({manifest['file'] or 'source not found under src/ or the root'})",
        f"build     {build_line}",
        f"hub       {hub_line}",
        f"items     {items['waiting_owner']} owner's move, {items['waiting_agent']} agent's move, "
        f"{items['done']} done, {items['dropped']} dropped",
        f"unread    agent {r['unread']['agent']}, owner {r['unread']['owner']}",
        f"views     {r['views']}",
    ]
    if items["unreadable"]:
        lines.append(f"unreadable  {', '.join(items['unreadable'])}")
    return "\n".join(lines)


@op("status", summary="Project, manifest, build id, hub and review item counts", human=_human_status)
def status(project: Project) -> dict:
    """What `wb` sees here: the project and its manifest, the build id, whether a hub runs, and how
    many review items wait on whom."""
    try:
        state = build_state(project.root)
        build = {
            "id": state.id,
            "dirty": state.dirty,
            "head": state.head,
            "changed": state.changed,
            "untracked": state.untracked,
            "error": None,
        }
    except GitError as exc:
        build = {"id": None, "dirty": None, "head": None, "changed": None, "untracked": None, "error": str(exc)}
    hub, why = running_hub(project)
    store = ReviewStore(project)
    states, unreadable = store.states()
    counts = {s: 0 for s in STATUSES}
    for s in states:
        counts[s["status"]] += 1
    return {
        "project": project.to_dict(),
        "manifest": {"module": project.manifest, "file": _manifest_file(project)},
        "build": build,
        "hub": {
            "running": hub is not None,
            "pid": hub.pid if hub else None,
            "port": hub.port if hub else None,
            "url": hub.url if hub else None,
            "reason": why or None,
        },
        "items": {**counts, "total": len(states), "unreadable": [name for name, _ in unreadable]},
        "unread": {
            "agent": sum(1 for s in states if s["unread_agent"]),
            "owner": sum(1 for s in states if s["unread_owner"]),
        },
        "views": len(store.view_ids()),
    }


def _human_snapshot(r: dict) -> str:
    verb = "created" if r["created"] else "exists already"
    return (
        f"{r['ref']} {verb}\n"
        f"commit {r['commit']} (tree {r['tree']}, parent {r['head'][:7]})\n"
        f"restore with: git worktree add --detach <dir> {r['ref']}"
    )


@op(
    "snapshot",
    summary="Commit the working tree to refs/wb/snap/<build id>; index, branch and files untouched",
    human=_human_snapshot,
)
def snapshot_op(project: Project) -> dict:
    """Store the exact working tree (tracked and untracked, not ignored) as a commit on top of HEAD
    under refs/wb/snap/<build id>, through a temporary index. Reuses the ref if it exists."""
    return snapshot(project.root)
