"""The decision round trip through the installed `wb` executable, one process per command, as the
agent and (until the UI exists) the owner run it: ask, answer, inbox returns it once, then nothing."""

from __future__ import annotations

import json
import subprocess

from hub_helpers import WB, project  # noqa: F401  (project: fixture)

LIMIT = "demo.params:RULES.front_limit"
QUESTIONS = [
    {
        "id": "q1",
        "text": "Which front limit?",
        "options": [{"id": "a", "label": "35 mm"}, {"id": "b", "label": "75 mm", "effect": {LIMIT: 75}}],
        "recommended": "a",
    },
    {"id": "q2", "text": "Rerun the study now?", "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]},
]


def wb(cwd, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(WB), *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def ok(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_ask_answer_inbox_round_trip(project, tmp_path):
    root = project.root
    asked = json.loads(ok(wb(root, "ask", "Front limit for the tipping study", "--questions", json.dumps(QUESTIONS),
                             "--body", "The 75 mm row used the 35 mm rule.", "--session", "s-1", "--json")))
    item = asked["id"]
    assert (item, asked["status"]) == ("R-0001", "waiting_owner")
    assert json.loads(ok(wb(root, "inbox", "--json")))["items"] == []  # the agent's own question is not news

    # The owner answers one of two questions, from another directory (-C finds the project).
    ok(wb(tmp_path, "-C", str(root), "answer", item, "--answers", '{"q1": {"choice": "b"}}', "--body", "Go with 75."))

    peek = ok(wb(root, "inbox", "--no-ack"))  # what the agent reads; nothing is acknowledged
    assert "owner answered" in peek and "q1 (Which front limit?) -> b: 75 mm" in peek
    assert "open questions: q2" in peek and "not acknowledged" in peek

    first = json.loads(ok(wb(root, "inbox", "--session", "s-1", "--json")))
    [entry] = first["items"]
    assert (entry["id"], entry["status"], entry["open_questions"]) == (item, "waiting_agent", ["q2"])
    [event] = entry["events"]
    assert (event["event"], event["by"], event["answers"], event["body"]) == (
        "answer", {"role": "owner"}, {"q1": {"choice": "b"}}, "Go with 75.")
    assert entry["approved"] == {LIMIT: 75}
    assert first["acknowledged"] is True

    assert json.loads(ok(wb(root, "inbox", "--json")))["items"] == []  # once, then nothing
    assert ok(wb(root, "inbox")).strip() == "inbox empty"

    events = json.loads(ok(wb(root, "item", item, "--events", "--json")))["events"]
    assert [(e["event"], e.get("to")) for e in events] == [("open", None), ("answer", None), ("delivered", "s-1")]

    bad = wb(root, "answer", item, "--answers", '{"q2": {"choice": "maybe"}}')
    assert bad.returncode == 1 and bad.stdout == ""
    assert bad.stderr.startswith("wb answer: ") and "'maybe' is not an option of q2" in bad.stderr
