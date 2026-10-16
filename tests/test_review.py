"""The review store: append-only item files, the fold, id allocation across processes, strict
validation, review/INDEX.md and saved views. Operations are called through the registry, the
same entry point the CLI and the hub's HTTP routes use."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import textwrap
import time

import pytest

from workbench.errors import InvalidInput, NotFound
from workbench.ops import default_registry
from workbench.review import ReviewStore
from workbench.review.schema import check_event
from workbench.review.yamlio import load_documents

LIMIT = "demo.params:RULES.front_limit"
QUESTIONS = [
    {
        "id": "q1",
        "text": "Which front limit?",
        "options": [
            {"id": "a", "label": "35 mm", "effect": {LIMIT: 35}},
            {"id": "b", "label": "75 mm", "effect": {LIMIT: 75}},
        ],
        "recommended": "a",
    },
    {"id": "q2", "text": "Why?", "options": [], "free_text": True},
]


def run(project, name: str, **arguments) -> dict:
    return default_registry().get(name).call(arguments, project=project)


def test_writes_only_ever_append(project, tmp_path):
    run(project, "ask", title="Front limit", questions=QUESTIONS)
    run(project, "note", kind="finding", title="Related")
    evidence = tmp_path / "table.csv"
    evidence.write_text("radius,footprint\n175,329\n")
    path = project.root / "review" / "R-0001.yaml"
    steps = [
        lambda: run(project, "answer", id="R-0001", answers={"q1": {"choice": "b"}}),
        lambda: run(project, "inbox"),
        lambda: run(project, "reply", id="R-0001", body="Applied 75 mm."),
        lambda: run(project, "attach", id="R-0001", source=str(evidence)),
        lambda: run(project, "link", id="R-0001", to="R-0002"),
        lambda: run(project, "set-status", id="R-0001", to="done", role="owner"),
    ]
    for step in steps:
        before = path.read_bytes()
        step()
        after = path.read_bytes()
        assert after.startswith(before) and len(after) > len(before)
    events = load_documents(path.read_text(), "R-0001.yaml")
    assert [e["event"] for e in events] == ["open", "answer", "delivered", "reply", "attach", "link", "status"]
    assert (project.root / "review/assets/R-0001/table.csv").read_text() == evidence.read_text()


def test_fold_partial_answers_approvals_moves_and_unread(project):
    s = run(project, "ask", title="Front limit", questions=QUESTIONS, session="s-1")
    assert (s["status"], s["open_questions"], s["unread_owner"], s["unread_agent"]) == (
        "waiting_owner", ["q1", "q2"], [0], [])

    # A partial answer: q1 only. The option's effect becomes an approved value.
    s = run(project, "answer", id="R-0001", answers={"q1": {"choice": "b"}})
    assert (s["status"], s["open_questions"], s["approved"]) == ("waiting_agent", ["q2"], {LIMIT: 75})
    assert (s["unread_agent"], s["unread_owner"]) == ([1], [])

    inbox = run(project, "inbox", session="s-1")
    assert [(i["id"], [e["index"] for e in i["events"]]) for i in inbox["items"]] == [("R-0001", [1])]
    assert ReviewStore(project).state("R-0001")["unread_agent"] == []

    s = run(project, "reply", id="R-0001", body="Applied 75 mm; q2 still open.")
    assert (s["status"], s["unread_owner"]) == ("waiting_owner", [3])

    # The rest, with an explicit approval that wins over the option's.
    s = run(project, "answer", id="R-0001", answers={"q2": {"text": "Doorways."}}, approves={LIMIT: 70, "x": True})
    assert (s["status"], s["open_questions"], s["approved"]) == ("waiting_agent", [], {LIMIT: 70, "x": True})
    assert (s["unread_agent"], s["unread_owner"]) == ([4], [])

    # A later answer to the same question replaces the earlier one; approvals merge in order.
    s = run(project, "answer", id="R-0001", answers={"q1": {"choice": "a"}})
    assert s["answers"]["q1"]["choice"] == "a"
    assert s["approved"] == {LIMIT: 35, "x": True}
    assert s["unread_agent"] == [4, 5]

    s = run(project, "set-status", id="R-0001", to="done")
    assert s["status"] == "done"
    assert s["last_activity"]["event"] == "status"


@pytest.mark.parametrize(
    ("by_role", "kind", "expected"),
    [
        ("agent", "comment", "waiting_owner"),
        ("agent", "measurement", "waiting_owner"),
        ("reviewer", "finding", "waiting_agent"),
        ("reviewer", "measurement", "waiting_owner"),
    ],
)
def test_whose_move_after_an_agent_or_reviewer_note(project, by_role, kind, expected):
    extra = {"quantity": "demo.params:BASE.wheel_width", "unit": "mm"} if kind == "measurement" else {}
    assert run(project, "note", kind=kind, title="t", role=by_role, **extra)["status"] == expected


def test_two_processes_never_get_the_same_id(project, tmp_path):
    per_process = 25
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from workbench.project import find_project
        from workbench.review import ReviewStore, make_by
        root, tag, n, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
        store = ReviewStore(find_project(root))
        Path(f"{go}.{tag}").touch()
        while not go.exists():
            time.sleep(0.001)
        for i in range(n):
            store.open_item({"by": make_by("agent", name=tag), "kind": "comment", "title": f"{tag} {i}"})
        """
    )
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(project.root), tag, str(per_process), str(go)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("p1", "p2")
    ]
    deadline = time.monotonic() + 30
    while not all((tmp_path / f"go.{tag}").exists() for tag in ("p1", "p2")):
        assert time.monotonic() < deadline, "workers did not start"
        time.sleep(0.01)
    go.touch()  # both start writing at once
    for p in procs:
        assert p.wait(timeout=120) == 0, p.stderr.read()

    store = ReviewStore(project)
    ids = store.item_ids()
    assert ids == [f"R-{n:04d}" for n in range(1, 2 * per_process + 1)]
    states, unreadable = store.states()
    assert unreadable == []
    titles = sorted(s["title"] for s in states)
    assert titles == sorted(f"{tag} {i}" for tag in ("p1", "p2") for i in range(per_process))
    assert all(s["event_count"] == 1 for s in states)
    index = (project.root / "review/INDEX.md").read_text()
    assert sum(line.startswith("- [R-") for line in index.splitlines()) == 2 * per_process


def test_an_inbox_running_while_the_owner_writes_loses_nothing(project, tmp_path):
    # Delivery is by position in the item file, not by time: owner events written in the same
    # second as a delivery must still arrive. And inbox reads the unread events and appends
    # `delivered` in one locked step, so an owner event cannot land between the two unreturned.
    run(project, "note", kind="comment", title="thread")
    replies = 40
    script = textwrap.dedent(
        """
        import json, sys, time
        from pathlib import Path
        from workbench.project import find_project
        from workbench.review import ReviewStore
        root, role, n, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
        store = ReviewStore(find_project(root))
        if role == "owner":
            for i in range(n):
                store.append("R-0001", "reply", {"by": {"role": "owner"}, "body": f"owner {i}"})
            out.touch()
        else:
            seen = []
            while True:
                finished = out.exists()
                for item in store.inbox(ack=True, session="agent")["items"]:
                    seen += [e["body"] for e in item["events"]]
                if finished:
                    break
                time.sleep(0.002)
            Path(f"{out}.seen").write_text(json.dumps(seen))
        """
    )
    done = tmp_path / "owner-done"
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(project.root), role, str(replies), str(done)],
                         stderr=subprocess.PIPE, text=True)
        for role in ("agent", "owner")
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0, p.stderr.read()
    seen = json.loads((tmp_path / "owner-done.seen").read_text())
    assert sorted(seen) == sorted(f"owner {i}" for i in range(replies))  # each exactly once
    events = ReviewStore(project).events("R-0001")
    assert sum(e["event"] == "delivered" for e in events) >= 2  # the inbox ran while the owner wrote


# -- strict validation ---------------------------------------------------------------------------

BASES = {
    "open": {
        "event": "open", "at": "2026-10-14T06:37:38Z", "by": {"role": "agent", "session": "s"}, "build": "5367af0",
        "kind": "decision", "title": "T", "body": "",
        "targets": [
            {"kind": "part", "label": "lower_plate", "assembly": "robot"},
            {"kind": "param", "key": "robot.params:BASE.wheel_width"},
            {"kind": "fact", "key": "mass.total"},
            {"kind": "file", "path": "src/robot/params.py", "line": 3},
            {"kind": "view", "id": "V-0001"},
        ],
        "questions": [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}], "recommended": "a",
                       "free_text": False}],
    },
    "answer": {"event": "answer", "at": "2026-10-14T06:37:38Z", "by": {"role": "owner"}, "build": "5367af0+0badcafe",
               "answers": {"q1": {"choice": "a"}}},
    "status": {"event": "status", "at": "2026-10-14T06:37:38Z", "by": {"role": "agent"}, "build": None, "to": "done"},
    "reply": {"event": "reply", "at": "2026-10-14T06:37:38Z", "by": {"role": "agent"}, "build": None, "body": "ok"},
    "delivered": {"event": "delivered", "at": "2026-10-14T06:37:38Z", "to": "s", "via": "inbox"},
    "attach": {"event": "attach", "at": "2026-10-14T06:37:38Z", "by": {"role": "owner"}, "build": None,
               "path": "review/assets/R-0001/photo.jpg"},
    "link": {"event": "link", "at": "2026-10-14T06:37:38Z", "by": {"role": "agent"}, "build": None, "to": "V-0002"},
}


def _set(path: str, value):
    def mutate(event: dict) -> None:
        *parents, last = path.split(".")
        node = event
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        if value is ...:
            del node[last]
        elif isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value

    return mutate


INVALID = [
    ("open", _set("targets.0.kind", "bolt"), "targets[0].kind: expected one of part, param, fact, file, view"),
    ("open", _set("targets.0.colour", "red"), "targets[0]: unknown field(s) colour"),
    ("open", _set("targets.0.label", ...), "targets[0]: missing field(s) label"),
    ("open", _set("targets.1.key", "BASE.wheel_width"), "targets[1].key: 'BASE.wheel_width' is not a parameter key"),
    ("open", _set("targets.3.line", "3"), "targets[3].line: expected an integer, got string"),
    ("open", _set("targets.3.line", True), "targets[3].line: expected an integer, got boolean"),
    ("open", _set("targets.3.line", 0), "targets[3].line: must be at least 1"),
    ("open", _set("targets.3.path", "/etc/passwd"), "targets[3].path: must be a path relative to the project root"),
    ("open", _set("targets.3.path", "src/../../x"), "targets[3].path: must be a normalised path inside the project"),
    ("open", _set("targets.4.id", "view-1"), "targets[4].id: 'view-1' is not a view id"),
    ("open", _set("questions.0.free_text", "false"), "questions[0].free_text: expected true or false, got string"),
    ("open", _set("questions.0.free_text", 0), "questions[0].free_text: expected true or false, got integer"),
    ("open", _set("questions.0.recommended", "z"), "questions[0].recommended: 'z' is not one of the option ids"),
    ("open", _set("questions.0.options.0.effect", 75), "questions[0].options[0].effect: expected text or a mapping"),
    ("open", _set("questions", []), "questions: must not be empty"),
    ("open", _set("kind", "question"), ".kind: expected one of comment, finding, decision"),
    ("open", _set("kind", "comment"), ".questions: only a decision has questions"),
    ("open", _set("quantity", "x"), ".quantity: only a measurement has quantity"),
    ("open", _set("title", "two\nlines"), ".title: must be a single line"),
    ("open", _set("title", "  "), ".title: must not be empty"),
    ("open", _set("by.role", "admin"), ".by.role: expected one of owner, agent, reviewer"),
    ("open", _set("by.extra", 1), ".by: unknown field(s) extra"),
    ("open", _set("build", "HEAD"), ".build: 'HEAD' is not a build id"),
    ("open", _set("at", "2026-10-14 06:37"), ".at: '2026-10-14 06:37' is not a UTC time"),
    ("open", _set("at", "2026-13-45T06:37:38Z"), ".at: '2026-13-45T06:37:38Z' is not a valid date and time"),
    ("open", _set("priority", "high"), ": unknown field(s) priority"),
    ("open", _set("event", "edit"), ".event: expected one of open, reply"),
    ("answer", _set("by.role", "agent"), ".by.role: only the owner answers"),
    ("answer", _set("answers", ...), ": an answer needs answers or approves"),
    ("answer", _set("answers.q1", {}), ".answers.q1: needs a choice or a text"),
    ("answer", _set("answers.q1.why", "x"), ".answers.q1: unknown field(s) why"),
    ("answer", _set("approves", {"k": float("nan")}), ".approves.k: must be finite"),
    ("status", _set("to", "closed"), ".to: expected one of waiting_owner"),
    ("reply", _set("status", "finished"), ".status: expected one of waiting_owner"),
    ("reply", _set("body", ""), ".body: must not be empty"),
    ("delivered", _set("by", {"role": "agent"}), ": unknown field(s) by"),
    ("attach", _set("path", "src/robot/params.py"), ".path: must be a file under review/assets/<item id>/"),
    ("attach", _set("path", "review/assets/R-0002/photo.jpg"), ".path: must be under review/assets/R-0001/"),
    ("link", _set("to", "R-0001"), ".to: an item cannot link to itself"),
    ("link", _set("to", "robot"), ".to: expected an item id R-... or a view id V-..."),
]


def test_valid_bases_pass():
    for base in BASES.values():
        check_event(copy.deepcopy(base), "event", "R-0001")


@pytest.mark.parametrize(("base", "mutate", "needle"), INVALID, ids=[case[2][:60] for case in INVALID])
def test_events_are_validated_strictly(base, mutate, needle):
    event = copy.deepcopy(BASES[base])
    mutate(event)
    with pytest.raises(InvalidInput) as excinfo:
        check_event(event, "event", "R-0001")
    assert needle in str(excinfo.value)


def test_rejected_events_never_reach_the_file(project):
    run(project, "ask", title="Front limit", questions=QUESTIONS)
    run(project, "note", kind="comment", title="Not a decision")
    files = {p: p.read_bytes() for p in (project.root / "review").glob("R-*.yaml")}
    bad = [
        ("R-0001", {"answers": {"q9": {"choice": "a"}}}, "no question 'q9'"),
        ("R-0001", {"answers": {"q1": {"choice": "z"}}}, "'z' is not an option of q1"),
        ("R-0001", {"answers": {"q1": {"text": "maybe"}}}, "q1 does not take free text"),
        ("R-0002", {"approves": {"k": 1}}, "a comment takes no answers"),
    ]
    for item_id, arguments, needle in bad:
        with pytest.raises(InvalidInput, match=needle):
            run(project, "answer", id=item_id, **arguments)
    with pytest.raises(NotFound, match="target view V-0009 does not exist"):
        run(project, "note", kind="comment", title="t", targets=[{"kind": "view", "id": "V-0009"}])
    with pytest.raises(NotFound, match="cannot link to R-0042"):
        run(project, "link", id="R-0001", to="R-0042")
    assert {p: p.read_bytes() for p in (project.root / "review").glob("R-*.yaml")} == files


# -- index and views ---------------------------------------------------------------------------

INDEX = """\
# Review items

Generated by `wbench index` from the item files in this directory; do not edit.

## Owner's move (1)

- [R-0002](R-0002.yaml) measurement: Wheel width (demo.params:BASE.wheel_width in mm; last by agent 2026-10-14 06:37)

## Agent's move (2)

- [R-0001](R-0001.yaml) decision: Front limit (1 of 2 questions open; last by owner 2026-10-14 06:37)
- [R-0003](R-0003.yaml) comment: Edge too close (last by owner 2026-10-14 06:37)

## Done (1)

- [R-0004](R-0004.yaml) finding: Old finding (last by agent 2026-10-14 06:37)

## Dropped (1)

- [R-0005](R-0005.yaml) change: Abandoned (last by owner 2026-10-14 06:37)
"""


def test_index_lists_every_item_once_grouped_by_whose_move(project, monkeypatch):
    monkeypatch.setattr("workbench.review.store.now", lambda: "2026-10-14T06:37:38Z")
    run(project, "ask", title="Front limit", questions=QUESTIONS)
    run(project, "note", kind="measurement", title="Wheel width", quantity="demo.params:BASE.wheel_width", unit="mm")
    run(project, "comment", title="Edge too close")
    run(project, "note", kind="finding", title="Old finding")
    run(project, "set-status", id="R-0004", to="done")
    run(project, "note", kind="change", title="Abandoned")
    run(project, "set-status", id="R-0005", to="dropped", role="owner")
    run(project, "answer", id="R-0001", answers={"q1": {"choice": "a"}})
    path = project.root / "review/INDEX.md"
    assert path.read_text() == INDEX  # written by every operation above, not only by `index`

    before = path.read_bytes()
    run(project, "inbox")  # a delivery is not news for the index
    assert path.read_bytes() == before
    assert run(project, "index")["changed"] is False

    (project.root / "review/R-0006.yaml").write_text("---\nevent: open\ntitle: [unclosed\n")
    result = run(project, "index")
    assert (result["items"], result["unreadable"]) == (5, ["R-0006.yaml"])
    text = path.read_text()
    assert "## Unreadable (1)\n\n- R-0006.yaml: review/R-0006.yaml: not valid YAML" in text
    assert text.count("- [R-") == 5


def test_views_round_trip_and_are_validated(project):
    camera = {"position": [400.0, -300.0, 250.0], "target": [0, 0, 120], "quaternion": [0.1, 0.2, 0.3, 0.9], "zoom": 1.5}
    section = {"planes": [{"normal": [0, 0, 1], "offset": 120.5}], "intersection": False}
    highlight = [{"kind": "part", "label": "lower_plate", "assembly": "robot"}]
    created = run(project, "view-create", assembly="robot", camera=camera, hidden=["camera_mast"], section=section,
                  highlight=highlight, note="Wheel clearance", role="owner")
    assert created["id"] == "V-0001"
    got = run(project, "view-get", id="V-0001")
    assert (got["assembly"], got["camera"], got["hidden"], got["section"], got["highlight"], got["note"]) == (
        "robot", camera, ["camera_mast"], section, highlight, "Wheel clearance")
    assert got["by"] == {"role": "owner"}
    assert [v["id"] for v in run(project, "view-list")["views"]] == ["V-0001"]
    # an item can now target the view
    assert run(project, "comment", title="See view", targets=[{"kind": "view", "id": "V-0001"}])["id"] == "R-0001"

    bad = [
        ({"camera": {**camera, "position": [1, 2]}}, "camera.position: expected 3 numbers, got 2"),
        ({"camera": {**camera, "zoom": 0}}, "camera.zoom: must be positive"),
        ({"camera": {**camera, "fov": 50}}, "camera: unknown field(s) fov"),
        ({"section": {"planes": [{"normal": [0, 0, 0], "offset": 1}]}}, "normal: must not be zero"),
        ({"highlight": [{"kind": "part"}]}, "highlight[0]: missing field(s) label"),
    ]
    for change, needle in bad:
        with pytest.raises(InvalidInput) as excinfo:
            run(project, "view-create", **{"assembly": "robot", "camera": camera, **change})
        assert needle in str(excinfo.value)
    assert run(project, "view-list")["views"][-1]["id"] == "V-0001"


def test_asked_evidence_is_frozen_with_the_item(project, tmp_path):
    table = tmp_path / "tipping.csv"
    table.write_text("radius,footprint\n175,329\n")
    s = run(project, "ask", title="Base radius", questions=QUESTIONS, attach=[str(table)])
    asset = "review/assets/R-0001/tipping.csv"
    assert s["evidence"] == [{"kind": "file", "path": asset}]
    table.write_text("radius,footprint\n175,999\n")  # the study is rerun later
    assert (project.root / asset).read_text() == "radius,footprint\n175,329\n"
