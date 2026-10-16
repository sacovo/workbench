"""The owner's terminal (`wbench owner`, `wbench todo`, `wbench have`): what waits on the owner is
listed and answered through the same operations as the page, and inventory records are appended.
The bom itself is read by the kernel (packages/kernel/tests/test_bom.py); here its answer is given."""

from __future__ import annotations

import json

import pytest

from hub_helpers import project  # noqa: F401  (fixture)
from test_jobs import run
from workbench import owner_cli
from workbench.ops import bom_ops
from workbench.review import ReviewStore

KEY = "robot.params:BASE.wheel_body_width"


@pytest.fixture
def items(project):
    store = ReviewStore(project)
    decision = store.open_item({
        "by": {"role": "agent"}, "kind": "decision", "title": "Which supply?",
        "questions": [{"id": "supply", "text": "How?", "options": [{"id": "a", "label": "Two rails"}, {"id": "b", "label": "One rail"}], "recommended": "a"}],
    })["id"]
    measurement = store.open_item({"by": {"role": "agent"}, "kind": "measurement", "title": "Hub ring", "quantity": KEY, "unit": "mm"})["id"]
    return decision, measurement


@pytest.fixture
def kernel(monkeypatch):
    stock = {"servo": 0.0}

    def fake(project, *arguments):
        row = {"id": "servo", "name": "Servo", "group": "Bought", "kind": "bought", "qty": 9.0, "have": stock["servo"], "recorded": True, "action": "to order"}
        row["short"] = max(0.0, row["qty"] - row["have"])
        return {"title": "Parts", "items": [row], "short": {"to order": int(row["short"] > 0)}}

    monkeypatch.setattr(bom_ops, "kernel_json", fake)
    return stock


def scripted(answers):
    replies = iter(answers)
    said = []
    return (lambda prompt: next(replies)), said.append, said


def test_todo_lists_the_owners_items_with_their_options(project, items):
    result = run(project, "todo")
    assert [s["id"] for s in result["items"]] == list(items)
    assert "a) Two rails  (recommended)" in bom_ops._human_todo(result)


def test_owner_answers_a_decision_with_the_recommendation_and_records_a_measurement(project, items, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    decision, measurement = items
    ask, say, _ = scripted(["1", "", "", "1", "36,1", "calipers, 3 readings", "q"])
    assert owner_cli.run(str(project.root), ask, say) == 0
    store = ReviewStore(project)
    answered = store.state(decision)
    assert answered["answers"]["supply"]["choice"] == "a" and answered["status"] == "waiting_agent"
    measured = store.state(measurement)
    assert measured["approved"] == {KEY: 36.1}


def test_owner_terminal_refuses_an_agent(project, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(owner_cli.NotForAgents):
        owner_cli.run(str(project.root), lambda p: "q", lambda s: None)


def test_have_appends_an_inventory_record(project, kernel):
    run(project, "have", id="servo", qty=4)
    run(project, "have", id="servo", qty=9, note="arrived")
    records = [json.loads(line) for line in (project.root / bom_ops.INVENTORY_FILE).read_text().splitlines()]
    assert [r["have"] for r in records] == [4, 9] and records[1]["note"] == "arrived" and records[0]["by"]["role"] == "owner"
    with pytest.raises(Exception):
        run(project, "have", id="nonsense", qty=1)
