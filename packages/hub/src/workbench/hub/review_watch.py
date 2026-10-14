"""What the page shows of `review/`: item states, saved views and comment pins, pushed on change.

Writers are the hub's own endpoints and any `wb` process (the agent's CLI writes the files
directly), so the hub watches the files rather than its own calls: it polls the names, sizes and
modification times of `review/`, `review/views/` and `review/assets/*/` twice a second, and reads
the store again only when that listing changed. A write through the hub also pokes the watcher,
so the page sees it without waiting for the next poll.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from workbench.project import Project
from workbench.review import ReviewStore

EVIDENCE_FILE = "evidence.json"  # a comment's frozen evidence, attached by the page's comment endpoint
THUMBNAIL_FILE = "thumbnail.png"


def listing(project: Project) -> tuple:
    """A cheap fingerprint of everything under review/ that the page shows."""
    root = project.root / "review"
    out = []
    for directory in (root, root / "views", *_asset_dirs(root)):
        try:
            entries = list(os.scandir(directory))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name == "INDEX.md":
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            out.append((str(Path(directory).relative_to(project.root)), entry.name, st.st_size, st.st_mtime_ns))
    return tuple(sorted(out))


def _asset_dirs(root: Path) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(root / "assets") if e.is_dir()]
    except (FileNotFoundError, NotADirectoryError):
        return []


def read_evidence(project: Project, state: dict) -> dict | None:
    """The frozen evidence of a comment made in the page, or None."""
    for path in state.get("attachments", []):
        if path.endswith("/" + EVIDENCE_FILE):
            try:
                data = json.loads((project.root / path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None
    return None


def snapshot(project: Project) -> dict:
    """The review message for the page: every item's folded state, the views and the pins."""
    store = ReviewStore(project)
    states, unreadable = store.states()
    views, bad_views = store.views()
    pins = []
    for state in states:
        evidence = read_evidence(project, state)
        if evidence and isinstance(evidence.get("point"), list) and len(evidence["point"]) == 3:
            pins.append(
                {
                    "item": state["id"],
                    "assembly": evidence.get("assembly"),
                    "point": evidence["point"],
                    "normal": evidence.get("normal"),
                    "status": state["status"],
                }
            )
    return {
        "type": "wb_review",
        "items": states,
        "unreadable": [{"file": name, "error": error} for name, error in unreadable + bad_views],
        "views": views,
        "pins": pins,
    }
