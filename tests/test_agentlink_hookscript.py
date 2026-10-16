"""The plugin's hook script, run exactly as Claude Code runs it: the command strings of
plugin/hooks/hooks.json through `/bin/sh -c`, the hook input on stdin. Inputs are the ones spike S2
recorded from Claude Code 2.1.283 (spikes/S2-agent-channels/run/recorded/); SessionStart was not
recorded there, and SESSION_START copies the fields 2.1.283 sent in a later run. The hub is a tiny one: the agent link's
routes behind uvicorn, with the review state held in memory as the real hub holds it.

Budgets (workbench.agentlink.budgets): with no hub every run stays under HOOK_DOWN_MAX_MS; with
the hub up, and with a hub that accepts and never answers, the p95 stays under HOOK_UP_P95_MS."""

from __future__ import annotations

import http.client
import json
import os
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from workbench.agentlink import AgentLink, NullTransport
from workbench.agentlink.budgets import HOOK_DOWN_MAX_MS, HOOK_UP_P95_MS
from workbench.agentlink.routes import routes
from workbench.ops import default_registry
from workbench.project import write_hub_info
from workbench.review import ReviewStore

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
RECORDED = REPO / "spikes" / "S2-agent-channels" / "run" / "recorded"
HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
TOKEN = "test-token-0123456789abcdef"
SID = "d685955c-98bf-407a-b841-432d8eaf96c3"  # the session of the recorded inputs
PANE = "wT:p4"

# The fields Claude Code 2.1.283 sent to the plugin's SessionStart hook in a `claude --init-only
# --plugin-dir plugin` run (2026-10-14), with S2's session id and directory.
SESSION_START = json.dumps(
    {
        "session_id": SID,
        "transcript_path": f"/home/sandro/.claude/projects/-home-sandro-work-privat-workbench-examples-minimal/{SID}.jsonl",
        "cwd": "/home/sandro/work/privat/workbench/examples/minimal",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
).encode()

# (hooks.json event, input): every event the plugin hooks, with S2's recorded input where there is one
CASES = {
    "session-start": ("SessionStart", SESSION_START),
    "user-prompt-submit": ("UserPromptSubmit", (RECORDED / "UserPromptSubmit.json").read_bytes()),
    "post-tool-use": ("PostToolUse", (RECORDED / "PostToolUse.json").read_bytes()),
    "stop": ("Stop", (RECORDED / "Stop-active0.json").read_bytes()),
}
STOP_ACTIVE = (RECORDED / "Stop-active1.json").read_bytes()


def run(project, name: str, **arguments) -> dict:
    return default_registry().get(name).call(arguments, project=project)


def command(event: str) -> str:
    (group,) = HOOKS[event]
    (handler,) = group["hooks"]
    return handler["command"]


def hook_env(root: Path, **extra: str) -> dict:
    # Not the environment of the Claude Code session running these tests.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "HERDR"))}
    env.update(CLAUDE_PLUGIN_ROOT=str(PLUGIN), CLAUDE_PROJECT_DIR=str(root), HERDR_PANE_ID=PANE, **extra)
    return env


def run_hook(event: str, stdin: bytes, env: dict) -> tuple[float, int, bytes, bytes]:
    t0 = time.perf_counter()
    p = subprocess.run(["/bin/sh", "-c", command(event)], input=stdin, capture_output=True, env=env, timeout=10)
    return (time.perf_counter() - t0) * 1000, p.returncode, p.stdout, p.stderr


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered)) - 1)]


class Server:
    """An ASGI app behind uvicorn on 127.0.0.1, in a thread."""

    def __init__(self, app):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "test server did not start"
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()


class Hub:
    """The agent link's routes with the review state in memory, refreshed when told (the real
    hub's review watcher does it on its own)."""

    def __init__(self, project):
        self.project = project
        self.items: list[dict] = []
        self.requests: list[tuple[str, dict]] = []
        self.pokes = 0
        self.link = AgentLink(project, lambda: self.items, NullTransport(), token=TOKEN, poke_review=self.poke, persist=False)
        handle = self.link.handle_hook

        def recording(event, payload):
            self.requests.append((event, payload))
            return handle(event, payload)

        self.link.handle_hook = recording
        self.refresh()

    def poke(self) -> None:
        self.pokes += 1

    def refresh(self) -> None:
        self.items = ReviewStore(self.project).states()[0]

    def get(self, path: str, token: str | None = TOKEN) -> tuple[int, dict]:
        return self.request("GET", path, None, token)

    def request(self, method: str, path: str, body: bytes | None, token: str | None = TOKEN) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Workbench-Token": token} if token else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data


@pytest.fixture
def hub(project):
    h = Hub(project)
    with Server(Starlette(routes=routes(h.link))) as server:
        h.port = server.port
        write_hub_info(project, port=server.port, token=TOKEN)
        yield h


def output_of(stdout: bytes) -> dict:
    assert stdout, "expected output"
    return json.loads(stdout)


def test_hooks_json_runs_the_script_for_four_events():
    assert set(HOOKS) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    for event, arg in [("SessionStart", "session-start"), ("UserPromptSubmit", "user-prompt-submit"),
                       ("PostToolUse", "post-tool-use"), ("Stop", "stop")]:
        (group,) = HOOKS[event]
        (handler,) = group["hooks"]
        assert handler == {
            "type": "command",
            "command": f'/usr/bin/python3 -I -S "${{CLAUDE_PLUGIN_ROOT}}/hooks/wb_hook.py" {arg}',
            "timeout": 5,
        }
        assert group.get("matcher", "*") == "*"


def test_recorded_inputs_against_a_running_hub(project, hub):
    env = hook_env(project.root)
    _, rc, out, err = run_hook("SessionStart", SESSION_START, env)
    assert (rc, out, err) == (0, b"", b"")
    session = hub.link.sessions[SID]
    assert (session.pane, session.source, session.cwd) == (PANE, "startup", "/home/sandro/work/privat/workbench/examples/minimal")

    event, stdin = CASES["user-prompt-submit"]
    assert run_hook(event, stdin, env)[1:] == (0, b"", b"")  # nothing waits
    run(project, "comment", title="TITLE-TOKEN edge", body="BODY-TOKEN")
    hub.refresh()
    _, rc, out, err = run_hook(event, stdin, env)
    ctx = output_of(out)["hookSpecificOutput"]
    assert (rc, err, ctx["hookEventName"]) == (0, b"", "UserPromptSubmit")
    assert 'R-0001 comment "TITLE-TOKEN edge" (owner opened)' in ctx["additionalContext"]
    assert "BODY-TOKEN" not in ctx["additionalContext"]

    event, stdin = CASES["post-tool-use"]
    assert run_hook(event, stdin, env)[1:] == (0, b"", b"")  # named already in this session
    run(project, "reply", id="R-0001", body="More", role="owner")
    hub.refresh()
    out = run_hook(event, stdin, env)[2]
    assert output_of(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    assert run_hook("Stop", STOP_ACTIVE, env)[1:] == (0, b"", b"")
    _, rc, out, err = run_hook("Stop", CASES["stop"][1], env)
    assert (rc, err) == (0, b"")
    assert output_of(out) == {"decision": "block", "reason": "Workbench: unread owner events on R-0001. Run: wbench inbox"}

    # what reached the hub: ids, names and flags, never the prompt text or the tool's input or output
    sent = json.dumps([payload for _, payload in hub.requests])
    assert "Repeat verbatim" not in sent and "sleep 8" not in sent and "tool_response" not in sent
    assert [e for e, _ in hub.requests] == [
        "session-start", "user-prompt-submit", "user-prompt-submit", "post-tool-use", "post-tool-use", "stop", "stop"]
    assert hub.requests[3][1] == {"session": SID, "pane": PANE, "cwd": "/home/sandro/work/privat/workbench/examples/minimal",
                                  "tool": "Bash", "pulled": False}
    assert hub.requests[-1][1]["stop_hook_active"] is False and hub.requests[-2][1]["stop_hook_active"] is True


def test_routes_check_the_token_event_and_body(project, hub):
    status, snap = hub.get("/api/agents")
    assert status == 200 and snap["transport"] == "none"
    assert hub.get("/api/agents", token=None)[0] == 401
    assert hub.get("/api/agents", token="wrong")[0] == 401
    ok = json.dumps({"session": SID, "pane": None, "cwd": None}).encode()
    assert hub.request("POST", "/api/hooks/user-prompt-submit", ok) == (200, {"output": None})
    assert hub.request("POST", "/api/hooks/user-prompt-submit", ok, token=None)[0] == 401
    assert hub.request("POST", "/api/hooks/pre-tool-use", ok)[0] == 404
    assert hub.request("POST", "/api/hooks/stop", b"{not json")[0] == 400
    assert hub.request("POST", "/api/hooks/stop", json.dumps({"session": "a b"}).encode())[0] == 400
    assert hub.request("POST", "/api/hooks/stop", b"x" * 70_000)[0] == 413


def test_the_script_itself_prints_only_well_formed_output_and_strict_stop_blocks(project):
    # A hub that answers every hook with a block, a malformed context or garbage: what the script
    # prints must not depend on the hub getting it right.
    answers = {"stop": {"decision": "block", "reason": "Workbench: unread owner events on R-0001. Run: wbench inbox"}}

    async def any_hook(request):
        event = request.path_params["event"]
        return JSONResponse({"output": answers.get(event)})

    with Server(Starlette(routes=[Route("/api/hooks/{event}", any_hook, methods=["POST"])])) as server:
        write_hub_info(project, port=server.port, token=TOKEN)
        env = hook_env(project.root)
        stop = json.loads(CASES["stop"][1])
        assert output_of(run_hook("Stop", json.dumps(stop).encode(), env)[2])["decision"] == "block"
        for value in (True, "false", "False", 0, None, [], {}):
            variant = json.dumps({**stop, "stop_hook_active": value}).encode()
            assert run_hook("Stop", variant, env)[1:] == (0, b"", b""), value
        missing = json.dumps({k: v for k, v in stop.items() if k != "stop_hook_active"}).encode()
        assert run_hook("Stop", missing, env)[1:] == (0, b"", b"")

        for bad in (
            {"decision": "block"},
            {"decision": "block", "reason": "x", "extra": 1},
            {"decision": "approve", "reason": "x"},
            {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "x"}},
        ):
            answers["stop"] = bad
            assert run_hook("Stop", CASES["stop"][1], env)[1:] == (0, b"", b""), bad
        for bad in (
            {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}},  # wrong event
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ""}},
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}, "continue": False},
            {"decision": "block", "reason": "x"},
        ):
            answers["user-prompt-submit"] = bad
            assert run_hook(*CASES["user-prompt-submit"], env)[1:] == (0, b"", b""), bad
        answers["user-prompt-submit"] = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ok"}}
        assert output_of(run_hook(*CASES["user-prompt-submit"], env)[2])["hookSpecificOutput"]["additionalContext"] == "ok"


def test_subagent_hooks_never_reach_the_hub(project, hub):
    run(project, "comment", title="Edge")
    hub.refresh()
    env = hook_env(project.root)
    for event, stdin in CASES.values():
        inside = json.dumps({**json.loads(stdin), "agent_id": "a1b2c3", "agent_type": "Explore"}).encode()
        assert run_hook(event, inside, env)[1:] == (0, b"", b"")
    assert hub.requests == []


def test_session_start_writes_the_env_file_with_or_without_a_hub(project, hub, tmp_path):
    env_file = tmp_path / "claude-env.sh"
    env = hook_env(project.root, CLAUDE_ENV_FILE=str(env_file))
    expected = f"unset VIRTUAL_ENV\nexport WB_SESSION={SID}\n"
    assert run_hook("SessionStart", SESSION_START, env)[1:] == (0, b"", b"")
    assert env_file.read_text() == expected
    assert run_hook("SessionStart", SESSION_START, env)[1:] == (0, b"", b"")
    assert env_file.read_text() == expected  # appended once
    for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
        run_hook(event, CASES[{"UserPromptSubmit": "user-prompt-submit", "PostToolUse": "post-tool-use", "Stop": "stop"}[event]][1], env)
    assert env_file.read_text() == expected  # only SessionStart writes it
    project.hub_json.unlink()
    other = tmp_path / "other-env.sh"
    other.write_text("export A=1")  # a line another hook left without a newline
    run_hook("SessionStart", SESSION_START, hook_env(project.root, CLAUDE_ENV_FILE=str(other)))
    assert other.read_text() == "export A=1\n" + expected
    shown = subprocess.run(
        ["bash", "-c", f'. "{other}"; echo "${{VIRTUAL_ENV-unset}} $WB_SESSION $A"'],
        capture_output=True, text=True, env={**os.environ, "VIRTUAL_ENV": "/home/sandro/.venv"},
    ).stdout.strip()
    assert shown == f"unset {SID} 1"


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.timing
def test_without_a_hub_every_hook_is_silent_and_under_the_budget(project):
    env = hook_env(project.root)
    scenarios = {
        "no hub.json": lambda: project.hub_json.unlink(missing_ok=True),
        "hub.json of a dead process": lambda: write_hub_info(project, port=_closed_port(), token=TOKEN, pid=_dead_pid()),
        "nothing on the port": lambda: write_hub_info(project, port=_closed_port(), token=TOKEN),
    }
    worst = {}
    for name, prepare in scenarios.items():
        prepare()
        for event, stdin in CASES.values():
            run_hook(event, stdin, env)  # warm-up
            times = []
            for _ in range(10):
                ms, rc, out, err = run_hook(event, stdin, env)
                assert (rc, out, err) == (0, b"", b""), (name, event)
                times.append(ms)
            worst[(name, event)] = max(times)
    print("\nhub down, worst of 10 (ms):", {f"{n} / {e}": round(v, 1) for (n, e), v in worst.items()})
    assert max(worst.values()) < HOOK_DOWN_MAX_MS, worst


@pytest.mark.timing
def test_with_the_hub_up_the_p95_is_under_the_budget(project, hub):
    for i in range(12):
        run(project, "comment", title=f"Owner comment {i}")
        run(project, "note", kind="finding", title=f"Agent finding {i}")
    hub.refresh()
    env = hook_env(project.root)
    stats = {}
    for event, stdin in [*CASES.values(), ("Stop", STOP_ACTIVE)]:
        run_hook(event, stdin, env)  # warm-up
        times = []
        for _ in range(30):
            ms, rc, out, err = run_hook(event, stdin, env)
            assert (rc, err) == (0, b"")
            if event in ("UserPromptSubmit", "Stop") and stdin is not STOP_ACTIVE:
                assert out, event  # a context, or the block: the hub was reached and answered
            times.append(ms)
        stats[event + (" (active)" if stdin is STOP_ACTIVE else "")] = (statistics.median(times), p95(times), max(times))
    print("\nhub up, p50/p95/max of 30 (ms):", {k: tuple(round(x, 1) for x in v) for k, v in stats.items()})
    assert max(v[1] for v in stats.values()) < HOOK_UP_P95_MS, stats


@pytest.mark.timing
def test_a_hub_that_never_answers_is_bounded_by_the_deadline(project):
    hung = socket.socket()
    hung.bind(("127.0.0.1", 0))
    hung.listen(512)  # accepts connections in the kernel, never answers
    try:
        write_hub_info(project, port=hung.getsockname()[1], token=TOKEN)
        env = hook_env(project.root)
        stats = {}
        for event, stdin in CASES.values():
            times = []
            for _ in range(10):
                ms, rc, out, err = run_hook(event, stdin, env)
                assert (rc, out, err) == (0, b"", b""), event
                times.append(ms)
            stats[event] = (statistics.median(times), p95(times), max(times))
    finally:
        hung.close()
    print("\nhub hung, p50/p95/max of 10 (ms):", {k: tuple(round(x, 1) for x in v) for k, v in stats.items()})
    assert max(v[1] for v in stats.values()) < HOOK_UP_P95_MS, stats
