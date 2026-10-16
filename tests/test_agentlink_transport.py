"""The herdr transport and the wake policy, against a fake `herdr` (and `claude`) on PATH that
records every argv and answers with canned JSON in the shapes spike S2 recorded from herdr 0.9.0
(`agent list`/`get`/`prompt`, `pane split`, `agent start`, `notification show`). The real herdr
is never called."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from workbench.agentlink import AgentLink, HerdrTransport, NullTransport, TransportError, make_transport
from workbench.agentlink.link import BURST, MAX_ATTEMPTS, POLL_WORKING
from workbench.ops import default_registry
from workbench.review import ReviewStore

SID = "d685955c-98bf-407a-b841-432d8eaf96c3"
PANE = "wT:p4"

FAKE_HERDR = r'''#!/usr/bin/python3
import json, os, sys
state_path = os.environ["FAKE_HERDR_STATE"]
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps({"prog": "herdr", "argv": args}) + "\n")
with open(state_path) as f:
    state = json.load(f)

def agent_json(a):
    out = {
        "agent": "claude",
        "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": a["session"]} if a.get("session") else None,
        "agent_status": a["status"], "cwd": "/tmp/project", "focused": False, "foreground_cwd": "/tmp/project",
        "interactive_ready": True, "pane_id": a["pane"], "revision": 4, "state_change_seq": a["seq"],
        "tab_id": "wT:t1", "terminal_id": "term_test", "terminal_title": "x", "terminal_title_stripped": "x",
        "workspace_id": "wT",
    }
    if a.get("name"):
        out["name"] = a["name"]
    return out

def ok(cmd, result):
    print(json.dumps({"id": "cli:" + cmd, "result": result}))
    sys.exit(0)

def fail(cmd, code, message="canned"):
    print(json.dumps({"error": {"code": code, "message": message}, "id": "cli:" + cmd}), file=sys.stderr)
    sys.exit(1)

def find(target):
    return next((a for a in state["agents"] if target in (a["pane"], a.get("name"))), None)

if args[:2] == ["agent", "list"]:
    ok("agent:list", {"agents": [agent_json(a) for a in state["agents"]], "type": "agent_list"})
if args[:2] == ["agent", "get"]:
    a = find(args[2])
    if a is None:
        fail("agent:get", "agent_not_found")
    ok("agent:get", {"agent": agent_json(dict(a, status=state.get("get_status") or a["status"])), "type": "agent_info"})
if args[:2] == ["agent", "prompt"]:
    a = find(args[2])
    if a is None:
        fail("agent:prompt", "agent_not_found")
    if a["status"] == "blocked":
        fail("agent:prompt", "agent_blocked", "agent is blocked and requires interactive input")
    if state.get("prompt_error"):
        fail("agent:prompt", state["prompt_error"])
    a["status"], a["seq"] = "done", a["seq"] + 2
    with open(state_path, "w") as f:
        json.dump(state, f)
    ok("agent:prompt", {"agent": agent_json(a), "type": "agent_prompted"})
if args[:2] == ["notification", "show"]:
    ok("notification:show", {"type": "ok"})
if args[:2] == ["pane", "split"]:
    ok("pane:split", {"pane": {"agent_status": "unknown", "pane_id": "wT:p9", "tab_id": "wT:t1", "workspace_id": "wT"}, "type": "pane_info"})
if args[:2] == ["agent", "start"]:
    if state.get("start_error"):
        fail("agent:start", state["start_error"], "blocked during startup")
    pane = args[args.index("--pane") + 1]
    ok("agent:start", {"agent": agent_json({"pane": pane, "status": "idle", "seq": 1, "name": args[2]}), "type": "agent_info"})
print("herdr: unknown command", file=sys.stderr)
sys.exit(2)
'''

FAKE_CLAUDE = r'''#!/usr/bin/python3
import json, os, sys
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps({"prog": "claude", "argv": sys.argv[1:]}) + "\n")
with open(os.environ["FAKE_HERDR_STATE"]) as f:
    state = json.load(f)
if sys.argv[1:] == ["agents", "--json"] and state.get("claude") is not None:
    print(json.dumps(state["claude"]))
    sys.exit(0)
sys.exit(1)
'''


class Fake:
    def __init__(self, root: Path):
        self.bin = root / "fakebin"
        self.bin.mkdir()
        for name, text in (("herdr", FAKE_HERDR), ("claude", FAKE_CLAUDE)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.state_path = root / "herdr-state.json"
        self.log_path = root / "herdr-calls.jsonl"
        self.log_path.write_text("")
        self.write({"agents": [], "claude": []})

    def read(self) -> dict:
        return json.loads(self.state_path.read_text())

    def write(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state))

    def agent(self, status: str, seq: int, pane: str = PANE, session: str | None = SID, claude: str | None = "idle", **extra):
        """Put one agent in herdr's list (replacing one in the same pane) and in `claude agents`."""
        state = self.read()
        state["agents"] = [a for a in state["agents"] if a["pane"] != pane] + [{"pane": pane, "status": status, "seq": seq, "session": session}]
        state["claude"] = [c for c in state["claude"] if c["sessionId"] != session]
        if claude is not None and session is not None:
            state["claude"].append({"pid": 1, "cwd": "/tmp/project", "kind": "interactive", "startedAt": 0,
                                    "sessionId": session, "name": "x", "status": claude})
        state.update(extra)
        self.write(state)

    def set(self, **extra) -> None:
        state = self.read()
        state.update(extra)
        self.write(state)

    def calls(self, prog: str = "herdr") -> list[list[str]]:
        lines = self.log_path.read_text().splitlines()
        return [c["argv"] for c in map(json.loads, lines) if c["prog"] == prog]

    def prompts(self) -> list[list[str]]:
        return [a for a in self.calls() if a[:2] == ["agent", "prompt"]]

    def notices(self) -> list[list[str]]:
        return [a for a in self.calls() if a[:2] == ["notification", "show"]]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def herdr(tmp_path, monkeypatch) -> Fake:
    fake = Fake(tmp_path)
    monkeypatch.setenv("FAKE_HERDR_STATE", str(fake.state_path))
    monkeypatch.setenv("FAKE_HERDR_LOG", str(fake.log_path))
    monkeypatch.setenv("PATH", f"{fake.bin}{os.pathsep}{os.environ['PATH']}")
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


def run(project, name: str, **arguments) -> dict:
    return default_registry().get(name).call(arguments, project=project)


def make_link(project, fake: Fake, clock=time.time) -> AgentLink:
    transport = HerdrTransport(str(fake.bin / "herdr"), str(fake.bin / "claude"))
    return AgentLink(project, lambda: ReviewStore(project).states()[0], transport, persist=False, clock=clock)


def register(link: AgentLink, session: str = SID, pane: str = PANE, prompted: bool = True) -> None:
    base = {"session": session, "pane": pane, "cwd": str(link.project.root)}
    link.handle_hook("session-start", {**base, "source": "startup"})
    if prompted:
        link.handle_hook("user-prompt-submit", base)
        link.handle_hook("stop", {**base, "stop_hook_active": False})


POINTER = "Workbench: owner opened R-0001. Run: wbench inbox"


def test_transport_reads_herdr_and_cross_checks_with_claude(herdr):
    herdr.agent("done", 1398, claude="idle")
    herdr.agent("working", 7, pane="wT:p5", session="another", claude="busy")
    herdr.agent("unknown", 3, pane="wT:p6", session=None)
    agents = {a.pane: a for a in HerdrTransport(str(herdr.bin / "herdr"), str(herdr.bin / "claude")).agents()}
    assert (agents[PANE].status, agents[PANE].session, agents[PANE].seq, agents[PANE].claude_status) == ("done", SID, 1398, "idle")
    assert (agents["wT:p5"].claude_status, agents["wT:p6"].session, agents["wT:p6"].claude_status) == ("busy", None, None)
    assert herdr.calls() == [["agent", "list"]] and herdr.calls("claude") == [["agents", "--json"]]


@pytest.mark.parametrize("status", ["idle", "done"])
def test_an_idle_or_done_agent_is_woken_once(project, herdr, clock, status):
    herdr.agent(status, 10)
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="TITLE-TOKEN", body="BODY-TOKEN")
    records = link.tick()
    assert herdr.prompts() == [["agent", "prompt", PANE, POINTER, "--wait", "--timeout", "15000"]]
    assert records[-1]["outcome"] == "sent" and records[-1]["items"] == {"R-0001": 0}
    assert ["agent", "get", PANE] in herdr.calls()  # the state is read again right before typing
    for _ in range(4):
        clock.advance(31)
        link.tick()
    assert len(herdr.prompts()) == 1
    assert link.snapshot()["waiting"][0]["pointed"] is True


def test_a_working_agent_is_left_to_the_hooks_then_woken_when_done(project, herdr, clock):
    herdr.agent("working", 10, claude="busy")
    link = make_link(project, herdr, clock)
    register(link)
    clock.advance(BURST)  # a later turn: no Stop hook in the last seconds
    run(project, "comment", title="Edge")
    link.tick()
    clock.advance(1)
    link.tick()  # not yet time to look again
    assert herdr.prompts() == [] and herdr.calls().count(["agent", "list"]) == 1
    herdr.agent("done", 12, claude="idle")  # ended without a Stop hook (Esc): no hook told the hub
    clock.advance(POLL_WORKING)
    link.tick()
    assert herdr.prompts() == [["agent", "prompt", PANE, POINTER, "--wait", "--timeout", "15000"]]


def test_a_stop_hook_that_holds_the_turn_back_is_the_delivery(project, herdr, clock):
    herdr.agent("working", 10)
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    out = link.handle_hook("stop", {"session": SID, "pane": PANE, "cwd": None, "stop_hook_active": False})
    assert out["decision"] == "block"
    herdr.agent("done", 12)
    for _ in range(3):
        clock.advance(11)
        link.tick()
    assert herdr.prompts() == []


def test_a_blocked_agent_is_never_typed_into_and_the_owner_is_told_once(project, herdr, clock):
    herdr.agent("blocked", 20, claude="waiting")
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    link.tick()
    for _ in range(3):
        clock.advance(2.5)
        link.tick()
    assert herdr.prompts() == []
    assert not [c for c in herdr.calls() if c[:2] == ["agent", "get"]]
    notices = herdr.notices()
    assert len(notices) == 1 and notices[0][2] == "Workbench: the agent waits at a prompt"
    assert "R-0001" in notices[0][4] and PANE in notices[0][4]
    herdr.agent("done", 23, claude="idle")  # the owner answered the prompt; the turn ended
    clock.advance(2.5)
    link.tick()
    assert herdr.prompts() == [["agent", "prompt", PANE, POINTER, "--wait", "--timeout", "15000"]]


def test_blocked_between_list_and_prompt_sends_nothing_until_the_state_changes(project, herdr, clock):
    herdr.agent("idle", 30, get_status="blocked")
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    records = link.tick()
    assert records[-1]["outcome"] == "skipped" and herdr.prompts() == []
    calls = len(herdr.calls())
    for _ in range(3):
        clock.advance(2.5)
        link.tick()  # herdr still lists the very state the wake was skipped in: no new attempt
    assert herdr.prompts() == [] and [c for c in herdr.calls()[calls:] if c[:2] != ["agent", "list"]] == []
    herdr.agent("idle", 33, get_status=None)
    clock.advance(2.5)
    link.tick()
    assert len(herdr.prompts()) == 1


def test_a_refused_prompt_is_not_retried_blindly(project, herdr, clock):
    herdr.agent("idle", 40, prompt_error="agent_blocked")
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    assert link.tick()[-1]["outcome"] == "refused_blocked"
    for _ in range(5):
        clock.advance(2.5)
        link.tick()
    assert len(herdr.prompts()) == 1
    # every new state that looks idle earns one attempt, and after MAX_ATTEMPTS the link gives up
    for seq in (50, 60, 70, 80):
        herdr.agent("idle", seq)
        clock.advance(2.5)
        link.tick()
    assert len(herdr.prompts()) == MAX_ATTEMPTS
    assert any(r.get("outcome") == "gave_up" for r in link.history)


@pytest.mark.parametrize("error, outcome", [("agent_prompt_stalled", "stalled"), ("timeout", "sent"), ("socket_closed", "failed")])
def test_a_prompt_that_may_have_arrived_is_never_repeated(project, herdr, clock, error, outcome):
    herdr.agent("idle", 50, prompt_error=error)
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    assert link.tick()[-1]["outcome"] == outcome
    for seq in (52, 54, 56):
        herdr.agent("done", seq, prompt_error=None)
        clock.advance(31)
        link.tick()
    assert len(herdr.prompts()) == 1


def test_claude_agents_overrules_an_idle_herdr(project, herdr, clock):
    herdr.agent("done", 60, claude="busy")
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    link.tick()
    assert herdr.prompts() == []
    herdr.agent("done", 60, claude="idle")
    clock.advance(POLL_WORKING)
    link.tick()
    assert len(herdr.prompts()) == 1


def test_only_registered_sessions_in_their_own_pane_are_woken(project, herdr, clock):
    herdr.agent("idle", 70, pane="wT:p5", session="unregistered")  # some other idle agent
    herdr.agent("idle", 71, pane=PANE, session="the-pane-moved-on")  # our pane, another conversation
    link = make_link(project, herdr, clock)
    register(link)
    run(project, "comment", title="Edge")
    link.tick()
    clock.advance(POLL_WORKING)
    link.tick()
    assert herdr.prompts() == []
    assert any(r.get("verdict") == "no_agent" for r in link.history)


def test_a_conversation_waiting_for_its_first_prompt_is_not_typed_into(project, herdr, clock):
    herdr.agent("idle", 80)
    link = make_link(project, herdr, clock)
    register(link, prompted=False)
    run(project, "comment", title="Edge")
    link.tick()
    assert herdr.prompts() == []
    link.handle_hook("user-prompt-submit", {"session": SID, "pane": PANE, "cwd": None})  # the owner wrote one
    clock.advance(POLL_WORKING)
    link.tick()
    assert len(herdr.prompts()) == 1


def test_the_owner_hears_of_a_new_decision_once(project, herdr, clock):
    run(project, "ask", title="Old question", questions=[{"id": "q", "text": "?", "options": [{"id": "a", "label": "A"}]}])
    link = make_link(project, herdr, clock)
    link.tick()  # what waits already at the first look is not news
    assert herdr.notices() == []
    run(project, "ask", title="Front limit", questions=[{"id": "q", "text": "?", "options": [{"id": "a", "label": "A"}]}])
    run(project, "note", kind="comment", title="Not a question")  # the owner's move, but asks nothing
    link.tick()
    link.tick()
    assert herdr.notices() == [["notification", "show", "Workbench: R-0002 waits for you", "--body", 'R-0002 decision "Front limit"']]


def test_spawn_splits_next_to_a_pane_without_focus_and_starts_the_agent(herdr, tmp_path, monkeypatch):
    transport = HerdrTransport(str(herdr.bin / "herdr"), None)
    out = transport.spawn("reviewer", tmp_path, "claude", ["--model", "haiku"], near=PANE)
    assert out == {"pane": "wT:p9", "name": "reviewer", "ready": True, "status": "idle"}
    assert herdr.calls() == [
        ["pane", "split", "--pane", PANE, "--direction", "right", "--cwd", str(tmp_path), "--no-focus"],
        ["agent", "start", "reviewer", "--kind", "claude", "--pane", "wT:p9", "--timeout", "60000", "--", "--model", "haiku"],
    ]
    herdr.set(start_error="agent_not_ready")
    blocked = transport.spawn("reviewer", tmp_path, "claude", near=PANE)
    assert blocked["ready"] is False and blocked["error"]["code"] == "agent_not_ready" and blocked["pane"] == "wT:p9"
    with pytest.raises(TransportError):
        transport.spawn("Bad Name", tmp_path, "claude", near=PANE)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    with pytest.raises(TransportError):
        transport.spawn("reviewer", tmp_path, "claude")


def test_wake_refuses_anything_but_a_one_line_pointer(herdr):
    transport = HerdrTransport(str(herdr.bin / "herdr"), None)
    from workbench.agentlink import AgentInfo

    for text in ("please read R-0001", "Workbench: two\nlines"):
        with pytest.raises(ValueError):
            transport.wake(AgentInfo(pane=PANE, status="idle"), text)
    assert herdr.calls() == []


def test_make_transport_finds_herdr_on_path(herdr, tmp_path, monkeypatch):
    t = make_transport()
    assert isinstance(t, HerdrTransport) and t.herdr == str(herdr.bin / "herdr") and t.claude == str(herdr.bin / "claude")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert isinstance(make_transport(), NullTransport)


def test_without_herdr_nothing_is_woken_and_hooks_still_work(project, clock):
    link = AgentLink(project, lambda: ReviewStore(project).states()[0], NullTransport(), persist=False, clock=clock)
    register(link, prompted=False)
    run(project, "comment", title="Edge")
    assert link.tick()[-1]["verdict"] == "no_agent"
    out = link.handle_hook("user-prompt-submit", {"session": SID, "pane": PANE, "cwd": None})
    assert "R-0001" in out["hookSpecificOutput"]["additionalContext"]


def test_the_link_thread_wakes_after_a_review_change(project, herdr):
    herdr.agent("done", 90)
    link = make_link(project, herdr)
    register(link)
    link.start()
    try:
        run(project, "comment", title="Edge")
        link.review_changed()
        deadline = time.monotonic() + 5
        while not herdr.prompts() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        link.stop()
    assert herdr.prompts() == [["agent", "prompt", PANE, POINTER, "--wait", "--timeout", "15000"]]
