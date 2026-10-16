"""Declared outputs as made: as-made stamps, and how each file stands against the code now.

A project declares the files it makes on its manifest (`wb.output(name, command, needs)`,
workbench_kernel.ledger); each lives at `<project out>/<name>`. When `wbench make` runs an output's
command as a job and the command succeeds, the runner stamps every declared output of that command
whose file the job wrote or changed: one JSON object per line in `ledger/made.jsonl`, appended and
never rewritten:

    {"output": "lower_plate.dxf", "path": "build/lower_plate.dxf", "sha256": "...", "bytes": 48213,
     "build": "<build id the job started from>", "job": "R-0140", "at": "2026-10-17T08:00:00Z",
     "values": {"robot.params:BASE.wheel_body_width": {"value": 36.1, "state": "measured", ...}}}

`values` are the assumed values the output rests on, as the ledger had them when the job started:
what the file was made with. The stamp is what a printed or cut part is traced back to.

An output's state (`status`), first match wins:
- missing: no file;
- unstamped: a file nobody stamped (written by running its script directly, or before stamps);
- modified: the file differs from its latest stamp (written again outside `wbench make`, or edited);
- stale: stamped at another build id than the project's now: the design files changed since, so
  the file may no longer match the code (the build id is the fingerprint; whether the change
  touches this file is not known without making it again);
- current: stamped at the current build id and unchanged.
Separately, `held` lists why the ledger would refuse to make it now; a current file can be held
when a measurement arrived after it was made.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from workbench.buildid import GitError, build_id
from workbench.project import Project
from workbench.review.store import make_by, now

MADE_FILE = Path("ledger") / "made.jsonl"
STATES = ("missing", "unstamped", "modified", "stale", "current")


def output_path(project: Project, name: str) -> Path:
    return project.root / project.out / name


def digest(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def read_stamps(project: Project) -> dict[str, dict]:
    """The latest stamp for each output (lines that are not JSON objects are skipped)."""
    path = project.root / MADE_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("output"), str):
            out[record["output"]] = record
    return out


def current_build(project: Project) -> str | None:
    try:
        return build_id(project.root)
    except GitError:
        return None


def status(project: Project, ledger: dict) -> dict:
    """Each declared output (from the kernel's ledger summary) with its file, stamp and state."""
    stamps = read_stamps(project)
    build = current_build(project)
    rows = []
    for declared in ledger["outputs"]:
        name = declared["name"]
        path = output_path(project, name)
        sha = digest(path)
        stamp = stamps.get(name)
        if sha is None:
            state = "missing"
        elif stamp is None:
            state = "unstamped"
        elif stamp.get("sha256") != sha:
            state = "modified"
        elif stamp.get("build") != build:
            state = "stale"
        else:
            state = "current"
        rows.append({
            **declared,
            "path": str(path.relative_to(project.root)),
            "exists": sha is not None,
            "bytes": path.stat().st_size if sha is not None else None,
            "state": state,
            "stamp": stamp,
        })
    return {"build": build, "outputs": rows}


def siblings(ledger: dict, name: str) -> list[str]:
    """The declared outputs one command makes (the one `name` names included)."""
    command = {o["name"]: o["command"] for o in ledger["outputs"]}[name]
    return [o["name"] for o in ledger["outputs"] if o["command"] == command]


def before(project: Project, names: list[str]) -> dict[str, str | None]:
    """The outputs' digests before a job runs (None: no file)."""
    return {name: digest(output_path(project, name)) for name in names}


def stamp_changed(project: Project, spec: dict) -> list[str]:
    """After a successful make job: stamp each of its outputs whose file it wrote or changed."""
    made = spec.get("make") or {}
    stamped = []
    lines = []
    for name, old in made.get("before", {}).items():
        path = output_path(project, name)
        sha = digest(path)
        if sha is None or sha == old:
            continue
        lines.append(json.dumps({
            "output": name,
            "path": str(path.relative_to(project.root)),
            "sha256": sha,
            "bytes": path.stat().st_size,
            "build": made.get("build"),
            "job": spec.get("item"),
            "at": now(),
            "by": make_by("runner"),
            "values": made.get("values", {}),
        }, ensure_ascii=False))
        stamped.append(name)
    if lines:
        with project.lock():
            target = project.root / MADE_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    return stamped
