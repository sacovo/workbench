"""Declared outputs as made (workbench.outputs): a make job stamps what it wrote, and each file's
state follows the stamp and the build id. The kernel's ledger answer is given (its own tests are in
packages/kernel); the job really runs, detached, in a throwaway git project."""

from __future__ import annotations

import json

from hub_helpers import git, project  # noqa: F401  (fixture)
from test_jobs import wait_for_end
from workbench import jobs, outputs
from workbench.buildid import build_id
from workbench.review.store import ReviewStore, make_by

LEDGER = {
    "assumptions": [{"key": "pkg.params:P.width", "item": "R-0001", "unit": "mm", "tolerance": 0.05, "note": "", "value": 36.4, "record": None, "state": "assumed"}],
    "outputs": [
        {"name": "plate.dxf", "command": "mkdir -p build && echo plate > build/plate.dxf && echo mount > build/mount.stl", "needs": [], "held": []},
        {"name": "mount.stl", "command": "mkdir -p build && echo plate > build/plate.dxf && echo mount > build/mount.stl", "needs": ["pkg.params:P.width"], "held": ["pkg.params:P.width is assumed"]},
        {"name": "other.stl", "command": "true", "needs": [], "held": []},
    ],
}


def states(project):
    return {o["name"]: o["state"] for o in outputs.status(project, LEDGER)["outputs"]}


def make(project, name):
    """What the make operation does after the ledger lets it: a job with the outputs of the command."""
    names = outputs.siblings(LEDGER, name)
    spec = {"outputs": names, "before": outputs.before(project, names), "build": outputs.current_build(project), "values": {"pkg.params:P.width": {"value": 36.4, "state": "assumed"}}}
    item = ReviewStore(project).open_item({"by": make_by("agent"), "kind": "job", "title": f"Make {name}"})["id"]
    command = {o["name"]: o["command"] for o in LEDGER["outputs"]}[name]
    jobs.start(project, item, command, make=spec)
    wait_for_end(project, item)
    return {"id": item, "report": ReviewStore(project).events(item)[-1]["body"]}


def test_a_make_job_stamps_each_output_its_command_wrote(project):
    assert states(project) == {"plate.dxf": "missing", "mount.stl": "missing", "other.stl": "missing"}
    ended = make(project, "plate.dxf")
    assert "Stamped as made: plate.dxf, mount.stl." in ended["report"]
    stamps = [json.loads(line) for line in (project.root / outputs.MADE_FILE).read_text().splitlines()]
    assert [s["output"] for s in stamps] == ["plate.dxf", "mount.stl"]
    assert stamps[0]["build"] == build_id(project.root) and stamps[0]["job"] == ended["id"]
    assert stamps[1]["values"]["pkg.params:P.width"]["value"] == 36.4
    # Writing the stamp and the job's review item leave the build id as it was: both are records.
    assert states(project) == {"plate.dxf": "current", "mount.stl": "current", "other.stl": "missing"}


def test_a_design_change_makes_a_file_stale_and_an_edit_marks_it_modified(project):
    make(project, "plate.dxf")
    (project.root / "src" / "demo" / "params.py").write_text("BASE = {'wheel_width': 61}\n")
    assert states(project)["plate.dxf"] == "stale"
    git(project.root, "checkout", "--", "src/demo/params.py")
    assert states(project)["plate.dxf"] == "current"
    (project.root / "build" / "plate.dxf").write_text("edited by hand\n")
    assert states(project)["plate.dxf"] == "modified"


def test_a_file_written_outside_make_is_unstamped_and_a_failed_job_stamps_nothing(project):
    (project.root / "build").mkdir()
    (project.root / "build" / "other.stl").write_text("solid\n")
    assert states(project)["other.stl"] == "unstamped"
    item = ReviewStore(project).open_item({"by": make_by("agent"), "kind": "job", "title": "Make plate.dxf"})["id"]
    spec = {"outputs": ["plate.dxf"], "before": {"plate.dxf": None}, "build": outputs.current_build(project), "values": {}}
    jobs.start(project, item, "mkdir -p build && echo half > build/plate.dxf && exit 3", make=spec)
    wait_for_end(project, item)
    report = ReviewStore(project).events(item)[-1]["body"]
    assert "exit 3" in report and "Stamped" not in report
    assert not (project.root / outputs.MADE_FILE).exists()
    assert states(project)["plate.dxf"] == "unstamped"
