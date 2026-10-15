"""Jobs (workbench.jobs, `wbench run`): a long command's end becomes the agent's move and wakes it,
like an owner's answer, and the owner sees it."""

from __future__ import annotations

import json
import time

import pytest

from hub_helpers import project  # noqa: F401  (fixture)
from test_agentlink_transport import PANE, herdr, make_link, register  # noqa: F401  (fixtures and helpers)
from workbench import jobs
from workbench.agentlink.texts import wake_pointer
from workbench.agentlink.waiting import waiting
from workbench.ops import default_registry
from workbench.review import ReviewStore


def run(project, name: str, **arguments) -> dict:
    return default_registry().get(name).call(arguments, project=project)


def wait_for_end(project, item_id: str, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        state = ReviewStore(project).state(item_id)
        if state["status"] != "running":
            return state
        time.sleep(0.1)
    raise AssertionError(f"{item_id} still running after {timeout} s")


def test_a_job_runs_detached_and_its_end_is_reported_as_the_agents_move(project):
    started = run(project, "run", title="Print and fail", command="echo HELLO-TOKEN; sleep 0.3; exit 3")
    item_id = started["id"]
    assert ReviewStore(project).state(item_id)["status"] == "running"
    state = wait_for_end(project, item_id)
    assert state["status"] == "waiting_agent"
    events = ReviewStore(project).events(item_id)
    report = events[-1]
    assert report["by"]["role"] == "runner"
    assert "exit 3" in report["body"] and "HELLO-TOKEN" in report["body"]
    # News for both sides: unread for the agent (it is due) and for the owner.
    assert state["unread_agent"] == [len(events) - 1] and state["unread_owner"]
    (due,) = [w for w in waiting([state]) if w.due]
    assert due.by == "runner" and due.news() == "job ended"
    assert wake_pointer([due]) == f"Workbench: job ended {item_id}. Run: wbench inbox"


def test_a_job_that_succeeds_says_so(project):
    item_id = run(project, "run", title="Quick", command="true")["id"]
    wait_for_end(project, item_id)
    assert ReviewStore(project).events(item_id)[-1]["body"].startswith("Finished: exit 0")


def test_a_finished_job_wakes_an_idle_agent(project, herdr):
    herdr.agent("idle", 10)
    link = make_link(project, herdr)
    register(link)
    item_id = run(project, "run", title="Study", command="exit 0")["id"]
    wait_for_end(project, item_id)
    records = link.tick()
    assert herdr.prompts() and herdr.prompts()[0][3] == f"Workbench: job ended {item_id}. Run: wbench inbox"
    assert records[-1]["outcome"] == "sent"


def test_a_job_whose_runner_died_is_reported_by_job_check(project):
    state = ReviewStore(project).open_item({"by": {"role": "agent"}, "kind": "job", "title": "Orphan"})
    jobs.jobs_dir(project).mkdir(parents=True, exist_ok=True)
    jobs.spec_path(project, state["id"]).write_text(json.dumps({"runner_pid": 2**22 + 12345}))
    assert run(project, "job-check")["reported"] == [state["id"]]
    assert ReviewStore(project).state(state["id"])["status"] == "waiting_agent"
    assert run(project, "job-check")["reported"] == []


def test_an_empty_command_is_refused(project):
    from workbench.errors import InvalidInput

    with pytest.raises(InvalidInput):
        run(project, "run", title="Nothing", command="  ")
