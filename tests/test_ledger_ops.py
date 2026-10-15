"""`wbench measure` and `wbench make` (workbench.ops.ledger_ops). The holds themselves are worked out
by the kernel (packages/kernel/tests/test_ledger.py); here the kernel's answer is given, and what
the hub owns is tested: the record it appends, the reply on the request, and that a held output
starts no job while a clear one runs its declared command."""

from __future__ import annotations

import json

import pytest

from hub_helpers import project  # noqa: F401  (fixture)
from test_jobs import run, wait_for_end
from workbench.errors import InvalidInput
from workbench.ops import ledger_ops
from workbench.review import ReviewStore

KEY = "robot.params:BASE.wheel_body_width"


@pytest.fixture
def request_item(project):
    return ReviewStore(project).open_item(
        {"by": {"role": "agent"}, "kind": "measurement", "title": "Hub ring width", "quantity": KEY, "unit": "mm"}
    )["id"]


@pytest.fixture
def kernel(monkeypatch, request_item):
    """The kernel's ledger answer: one assumption, one output, holds as set by the test."""
    answer = {"holds": []}

    def fake(project, output=None):
        result = {
            "assumptions": [{"key": KEY, "item": request_item, "unit": "mm", "value": 36.4, "state": "assumed", "record": None}],
            "outputs": [{"name": "lower_plate.dxf", "command": "echo MADE > made.txt", "needs": [KEY], "held": answer["holds"]}],
        }
        if output is not None:
            result["holds"] = answer["holds"]
        return result

    monkeypatch.setattr(ledger_ops, "kernel_ledger", fake)
    return answer


def records(project):
    return [json.loads(line) for line in (project.root / ledger_ops.LEDGER_FILE).read_text().splitlines()]


def test_measure_appends_a_record_and_replies_on_the_request(project, kernel, request_item):
    run(project, "measure", key=KEY, value=36.1, source="calipers, three readings")
    run(project, "measure", key=KEY, value=36.2, source="again")
    first, second = records(project)
    assert (first["value"], first["unit"], first["item"], first["by"]["role"]) == (36.1, "mm", request_item, "agent")
    assert second["value"] == 36.2  # appended, the first kept
    assert "36.1 mm" in ReviewStore(project).events(request_item)[-2]["body"]


def test_measure_takes_the_owners_answer(project, kernel, request_item):
    ReviewStore(project).append(request_item, "answer", {"by": {"role": "owner"}, "approves": {KEY: 36.05}})
    run(project, "measure", key=KEY)
    (only,) = records(project)
    assert only["value"] == 36.05 and request_item in only["source"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"key": "robot.params:BASE.other", "value": 1.0, "source": "s"},  # not declared
        {"key": KEY, "value": 36.1, "unit": "cm", "source": "s"},  # wrong unit
        {"key": KEY, "value": 36.1},  # no source
        {"key": KEY},  # no value and no owner's answer
    ],
)
def test_measure_refuses_what_it_cannot_record(project, kernel, arguments):
    with pytest.raises(InvalidInput):
        run(project, "measure", **arguments)
    assert not (project.root / ledger_ops.LEDGER_FILE).exists()


def test_make_refuses_a_held_output_and_starts_nothing(project, kernel, request_item):
    kernel["holds"] = [f"{KEY} is assumed (36.4 mm) and not measured yet: {request_item}"]
    before = len(ReviewStore(project).states()[0])
    with pytest.raises(ledger_ops.OutputHeld, match=request_item):
        run(project, "make", output="lower_plate.dxf")
    assert len(ReviewStore(project).states()[0]) == before and not (project.root / "made.txt").exists()


def test_make_runs_a_clear_output_as_a_job(project, kernel):
    started = run(project, "make", output="lower_plate.dxf")
    wait_for_end(project, started["id"])
    assert (project.root / "made.txt").read_text().strip() == "MADE"
