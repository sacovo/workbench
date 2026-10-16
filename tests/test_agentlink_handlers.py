"""The agent link's hook handling, against a throwaway project with real review items written
through the operation registry (the same entry point as the CLI and the hub). The hub's memory of
review/ is stood in for by reading the store on each call."""

from __future__ import annotations

import json

import pytest

from workbench.agentlink import AgentLink
from workbench.agentlink.link import STATE_FILE
from workbench.agentlink.texts import clean_title, context, stop_reason, wake_pointer
from workbench.agentlink.waiting import waiting
from workbench.errors import InvalidInput, NotFound
from workbench.ops import default_registry
from workbench.review import ReviewStore

SID = "d685955c-98bf-407a-b841-432d8eaf96c3"  # the session in spike S2's recorded hook inputs
LIMIT = "demo.params:RULES.front_limit"
QUESTIONS = [
    {
        "id": "q1",
        "text": "QUESTION-TOKEN which front limit?",
        "options": [
            {"id": "a", "label": "LABEL-TOKEN 35 mm", "effect": {LIMIT: 35}},
            {"id": "b", "label": "75 mm", "effect": {LIMIT: 75}},
        ],
    },
    {"id": "q2", "text": "Why?", "options": [], "free_text": True},
]


def run(project, name: str, **arguments) -> dict:
    return default_registry().get(name).call(arguments, project=project)


def make_link(project, **kw) -> AgentLink:
    return AgentLink(project, lambda: ReviewStore(project).states()[0], persist=kw.pop("persist", False), **kw)


def hook(link: AgentLink, event: str, session: str = SID, **fields):
    return link.handle_hook(event, {"session": session, "pane": "wC:p4", "cwd": str(link.project.root), **fields})


def context_of(output: dict | None, event: str) -> str:
    assert output is not None, f"expected {event} context"
    assert set(output) == {"hookSpecificOutput"}
    spec = output["hookSpecificOutput"]
    assert set(spec) == {"hookEventName", "additionalContext"} and spec["hookEventName"] == event
    return spec["additionalContext"]


def test_nothing_waits_nothing_is_said(project):
    link = make_link(project)
    assert hook(link, "session-start", source="startup") is None
    assert hook(link, "user-prompt-submit") is None
    assert hook(link, "post-tool-use", tool="Bash") is None
    assert hook(link, "stop", stop_hook_active=False) is None
    run(project, "note", kind="comment", title="An agent's own note")  # the owner's move, not the agent's
    assert hook(link, "user-prompt-submit") is None
    assert hook(link, "stop", stop_hook_active=False) is None


def test_owner_comment_is_pointed_at_without_its_content(project):
    run(project, "comment", title="TITLE-TOKEN bracket edge too thin", body="BODY-TOKEN the fillet is 0.5 mm")
    link = make_link(project)
    text = context_of(hook(link, "user-prompt-submit"), "UserPromptSubmit")
    assert text.startswith("Workbench: 1 item has owner events")
    assert 'R-0001 comment "TITLE-TOKEN bracket edge too thin" (owner opened)' in text
    assert text.endswith("Run: wbench inbox")
    assert "BODY-TOKEN" not in text


def test_answer_is_pointed_at_with_the_verb_not_the_choice(project):
    run(project, "ask", title="Front limit", questions=QUESTIONS, session=SID)
    link = make_link(project)
    assert hook(link, "user-prompt-submit") is None  # the agent asked: the owner's move
    run(project, "answer", id="R-0001", answers={"q1": {"choice": "a"}, "q2": {"text": "ANSWER-TOKEN cheaper"}},
        body="REMARK-TOKEN")
    text = context_of(hook(link, "user-prompt-submit"), "UserPromptSubmit")
    assert 'R-0001 decision "Front limit" (owner answered)' in text
    for token in ("QUESTION-TOKEN", "LABEL-TOKEN", "ANSWER-TOKEN", "REMARK-TOKEN", "35"):
        assert token not in text


def test_post_tool_use_names_each_owner_event_once(project):
    run(project, "comment", title="Edge")
    link = make_link(project)
    assert context_of(hook(link, "post-tool-use", tool="Bash"), "PostToolUse")
    assert hook(link, "post-tool-use", tool="Read") is None  # already named to this session
    assert hook(link, "post-tool-use", tool="Bash") is None
    run(project, "reply", id="R-0001", body="One more thing", role="owner")  # a new owner event
    text = context_of(hook(link, "post-tool-use", tool="Edit"), "PostToolUse")
    assert "(owner replied to)" in text
    assert hook(link, "post-tool-use", tool="Edit") is None
    # another session in the project has not been told yet
    assert context_of(hook(link, "post-tool-use", session="other-session", tool="Bash"), "PostToolUse")


def test_the_prompt_hook_repeats_but_a_typed_pointer_gets_no_context(project):
    run(project, "comment", title="Edge")
    link = make_link(project)
    assert hook(link, "user-prompt-submit", pointer_prompt=True) is None  # the typed text already points
    assert hook(link, "post-tool-use", tool="Bash") is None  # and counts as named
    assert context_of(hook(link, "user-prompt-submit"), "UserPromptSubmit")  # a new turn is reminded


def test_wbench_inbox_call_is_silent_and_refreshes_the_snapshot(project):
    pokes = []
    run(project, "comment", title="Edge")
    link = make_link(project, poke_review=lambda: pokes.append(1))
    assert hook(link, "post-tool-use", tool="Bash", pulled=True) is None
    assert pokes == [1]
    run(project, "inbox", session=SID)
    assert hook(link, "post-tool-use", tool="Bash") is None
    assert hook(link, "stop", stop_hook_active=False) is None


def test_stop_blocks_only_on_the_json_boolean_false(project):
    run(project, "comment", title="TITLE-TOKEN", body="BODY-TOKEN")
    run(project, "comment", title="Second")
    link = make_link(project)
    out = hook(link, "stop", stop_hook_active=False)
    assert out == {"decision": "block", "reason": "Workbench: unread owner events on R-0001, R-0002. Run: wbench inbox"}
    assert link.sessions[SID].turn == "continuing"
    for value in (True, "false", "False", 0, None, [], {}):
        assert hook(link, "stop", stop_hook_active=value) is None, value
    assert link.handle_hook("stop", {"session": SID, "pane": None, "cwd": None}) is None  # field missing


def test_stop_ignores_owner_events_that_are_not_the_agents_move(project):
    run(project, "note", kind="comment", title="Ready for a look")
    run(project, "set-status", id="R-0001", to="done", role="owner")  # an owner event, nobody's move
    link = make_link(project)
    assert "(owner set the status of)" in context_of(hook(link, "user-prompt-submit"), "UserPromptSubmit")
    assert hook(link, "stop", stop_hook_active=False) is None


def test_session_start_registers_and_resets(project):
    link = make_link(project)
    hook(link, "session-start", source="startup", agent_type=None, herdr_socket="/run/herdr.sock")
    s = link.sessions[SID]
    assert (s.pane, s.cwd, s.source, s.herdr_socket, s.turns) == ("wC:p4", str(project.root), "startup", "/run/herdr.sock", 0)
    run(project, "comment", title="Edge")
    hook(link, "user-prompt-submit")
    assert s.turns == 1 and s.announced == {"R-0001": 0}
    hook(link, "session-start", source="compact")
    assert s.turns == 1 and s.announced == {}  # named again after a compaction
    hook(link, "session-start", source="clear")
    assert s.turns == 0
    # a pane hosts one conversation: a new session there replaces the old one
    hook(link, "session-start", session="new-session", source="startup")
    assert set(link.sessions) == {"new-session"}
    # a session first seen mid-conversation (hub restarted) has had prompts
    hook(link, "post-tool-use", session="mid-turn", tool="Bash")
    assert link.sessions["mid-turn"].turns == 1


def test_malformed_hook_bodies_are_refused(project):
    link = make_link(project)
    with pytest.raises(NotFound):
        link.handle_hook("pre-tool-use", {"session": SID})
    bad = [
        ("user-prompt-submit", []),
        ("user-prompt-submit", {"pane": "wC:p4"}),
        ("user-prompt-submit", {"session": "has space"}),
        ("user-prompt-submit", {"session": SID, "pane": "w C"}),
        ("user-prompt-submit", {"session": SID, "prompt": "text must never be sent"}),
        ("user-prompt-submit", {"session": SID, "pointer_prompt": "yes"}),
        ("post-tool-use", {"session": SID, "pulled": 1}),
        ("session-start", {"session": SID, "herdr_socket": "relative/path"}),
        ("session-start", {"session": SID, "cwd": "two\nlines"}),
        ("stop", {"session": SID, "tool": "Bash"}),
    ]
    for event, payload in bad:
        with pytest.raises(InvalidInput):
            link.handle_hook(event, payload)


def test_texts_carry_ids_and_titles_never_content(project):
    secret = {"BODY-TOKEN", "QUESTION-TOKEN", "LABEL-TOKEN", "ANSWER-TOKEN", "REMARK-TOKEN", "REPLY-TOKEN"}
    run(project, "ask", title="TITLE-TOKEN-1 front limit", questions=QUESTIONS, body="BODY-TOKEN")
    run(project, "answer", id="R-0001", answers={"q1": {"choice": "a"}, "q2": {"text": "ANSWER-TOKEN"}}, body="REMARK-TOKEN")
    run(project, "comment", title="TITLE-TOKEN-2 edge", body="BODY-TOKEN")
    run(project, "note", kind="finding", title="TITLE-TOKEN-3 clearance")
    run(project, "reply", id="R-0003", body="REPLY-TOKEN", role="owner")
    unread = waiting(ReviewStore(project).states()[0])
    assert [w.id for w in unread] == ["R-0001", "R-0002", "R-0003"]
    assert [w.event for w in unread] == ["answer", "open", "reply"]
    pointer, reason, ctx = wake_pointer(unread), stop_reason(unread), context(unread)
    assert pointer == "Workbench: owner answered R-0001; owner opened R-0002; owner replied to R-0003. Run: wbench inbox"
    for text in (pointer, reason):
        assert "TITLE-TOKEN" not in text and not any(t in text for t in secret)
        assert "\n" not in text
    assert all(f"TITLE-TOKEN-{i}" in ctx for i in (1, 2, 3))  # titles are allowed in the hook context
    assert not any(t in ctx for t in secret)


def test_texts_are_bounded(project):
    for i in range(8):
        run(project, "comment", title=f'Title {i} with "quotes"\tand a very long tail ' + "x" * 200)
    unread = waiting(ReviewStore(project).states()[0])
    assert wake_pointer(unread).endswith("owner opened R-0005 and 3 more. Run: wbench inbox")
    assert stop_reason(unread) == "Workbench: unread owner events on R-0001, R-0002, R-0003, R-0004, R-0005 and 3 more. Run: wbench inbox"
    ctx = context(unread)
    assert ctx.startswith("Workbench: 8 items have owner events") and "and 3 more. Run: wbench inbox" in ctx
    assert len(ctx) < 1000 and '"quotes"' not in ctx and "\t" not in ctx
    assert clean_title("a\nb\x00c" + "y" * 100).startswith("a b c") and len(clean_title("y" * 200)) == 80


def test_state_survives_a_restart_and_is_not_in_review(project):
    run(project, "comment", title="Edge")
    link = make_link(project, persist=True)
    hook(link, "session-start", source="startup")
    hook(link, "user-prompt-submit")
    assert hook(link, "stop", stop_hook_active=False)["decision"] == "block"
    link.tick()  # writes .workbench/
    saved = json.loads((project.state_dir / STATE_FILE).read_text())
    assert saved["pointed"] == {"R-0001": 0}
    log_lines = (project.logs_dir / "agentlink.jsonl").read_text().splitlines()
    assert json.loads(log_lines[0])["what"] == "session"
    again = make_link(project, persist=True)
    assert again.pointed == {"R-0001": 0}
    assert again.sessions[SID].pane == "wC:p4" and again.sessions[SID].turns == 1
    assert sorted(p.name for p in (project.root / "review").iterdir()) == ["INDEX.md", "R-0001.yaml"]


def test_snapshot_lists_sessions_and_waiting_items(project):
    run(project, "comment", title="Edge")
    link = make_link(project)
    hook(link, "session-start", source="startup")
    snap = link.snapshot()
    assert snap["transport"] == "none"
    assert [s["session"] for s in snap["sessions"]] == [SID] and snap["sessions"][0]["pane"] == "wC:p4"
    assert snap["waiting"] == [
        {"id": "R-0001", "kind": "comment", "title": "Edge", "status": "waiting_agent", "event": "open", "unread": 1,
         "due": True, "pointed": False}
    ]
    json.dumps(snap)
