"""The agent link mounted in the real hub: create_app(hub, extra_routes=routes(link)) behind its
Guard (Host, Origin, token), with the fake kernel, the link reading the hub's own in-memory review
state, and the plugin's hook script calling it as Claude Code would. This is the wiring that
server.run() needs (see workbench.agentlink.link, "What the hub provides"), including the
turn_ended callback that rebuilds a stale scene once a registered session's turn ends."""

from __future__ import annotations

import http.client
import json
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hub_helpers import project  # noqa: F401  (fixture)
from hub_running import fake_control
from test_agentlink_hookscript import CASES, SID, STOP_ACTIVE, hook_env, run_hook
from workbench.agentlink import AgentLink, NullTransport
from workbench.agentlink.routes import routes
from workbench.hub.app import create_app
from workbench.hub.hub import Hub
from workbench.hub.server import bind
from workbench.project import write_hub_info

FAKE_KERNEL = Path(__file__).with_name("fake_kernel.py")


def wait_until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


class LinkedHub:
    def __init__(self, project):
        self.project = project
        self.token = secrets.token_urlsafe(16)
        self.page_credential = secrets.token_urlsafe(16)  # what a page `wbench open` opened would carry
        sock = bind(0)
        self.port = sock.getsockname()[1]
        command = [sys.executable, str(FAKE_KERNEL), "serve", "--project", str(project.root)]
        self.hub = Hub(project, self.token, self.port, page_credential=self.page_credential, kernel_command=command,
                       auto_show=False, review_poll=0.1, code_poll=0.1)
        self.link = AgentLink(project, lambda: self.hub.review.get("items", []), NullTransport(), token=self.token,
                              poke_review=self.hub.poke_review, turn_ended=self.hub._agent_turn_ended)
        self.hub.link = self.link  # as server.run() wires it

        async def started():
            self.link.start()

        async def stopped():
            self.link.stop()

        app = create_app(self.hub, extra_routes=routes(self.link), on_started=started, on_stopped=stopped)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        wait_until(lambda: self.server.started, 20)
        write_hub_info(project, port=self.port, token=self.token)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=None if body is None else json.dumps(body),
                     headers={"X-Workbench-Token": self.token, "X-Workbench-Page-Credential": self.page_credential,
                               "Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = json.loads(response.read() or b"null")
        conn.close()
        return response.status, data

    def items(self) -> list[dict]:
        return self.hub.review.get("items", [])

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)


@pytest.fixture
def linked(project):
    h = LinkedHub(project)
    yield h
    h.stop()


def test_hooks_through_the_real_hub_and_its_guard(project, linked):
    env = hook_env(project.root)
    assert run_hook("SessionStart", CASES["session-start"][1], env)[1:] == (0, b"", b"")
    assert linked.link.sessions[SID].pane == "wT:p4"

    status, opened = linked.request("POST", "/api/op/comment", {"title": "Edge too thin", "body": "BODY-TOKEN"})
    assert status == 200 and opened["id"] == "R-0001"
    wait_until(lambda: any(i["id"] == "R-0001" for i in linked.items()))
    _, rc, out, err = run_hook(*CASES["user-prompt-submit"], env)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert (rc, err) == (0, b"") and 'R-0001 comment "Edge too thin" (owner opened)' in ctx and "BODY-TOKEN" not in ctx
    assert json.loads(run_hook(*CASES["stop"], env)[2]) == {
        "decision": "block", "reason": "Workbench: unread owner events on R-0001. Run: wbench inbox"}
    assert run_hook("Stop", STOP_ACTIVE, env)[1:] == (0, b"", b"")

    # the agent pulls: `wbench inbox` (here through the hub's op route) records the delivery
    status, inbox = linked.request("POST", "/api/op/inbox", {"session": SID})
    assert status == 200 and [i["id"] for i in inbox["items"]] == ["R-0001"]
    wait_until(lambda: not linked.items()[0]["unread_agent"])
    assert run_hook(*CASES["stop"], env)[1:] == (0, b"", b"")

    status, snap = linked.request("GET", "/api/agents")
    assert status == 200 and snap["sessions"][0]["session"] == SID and snap["waiting"] == []
    assert linked.request("GET", "/api/agents", headers={"X-Workbench-Token": "wrong"})[0] == 401
    assert linked.request("GET", "/api/agents", headers={"Origin": "http://evil.example"})[0] == 403
    assert linked.request("GET", "/api/agents", headers={"Host": f"evil.example:{linked.port}"})[0] == 403

    # workbench.ops.hub_ops.agents (needs_hub=True) returns exactly the same snapshot.
    status, via_op = linked.request("POST", "/api/op/agents", {})
    status2, via_route = linked.request("GET", "/api/agents")
    assert (status, status2) == (200, 200) and via_op == via_route


def test_a_stale_scene_rebuilds_in_the_background_once_a_registered_sessions_turn_ends(project, linked):
    """handle_hook("stop", ...) for a registered session, with the hub's scene stale (code
    changed), calls Hub._agent_turn_ended through AgentLink.turn_ended. The hook's own answer must
    not wait for the rebuild, and a second Stop while one is already running must not start another."""
    status, shown = linked.request("POST", "/api/op/show", {"assembly": "demo"})
    assert status == 200
    old_pid = linked.request("GET", "/api/state")[1]["kernel"]["pid"]

    env = hook_env(project.root)
    assert run_hook("SessionStart", CASES["session-start"][1], env)[1:] == (0, b"", b"")
    assert SID in linked.link.sessions

    params = project.root / "src" / "demo" / "params.py"
    params.write_text(params.read_text() + "LIFT = {'stroke': 400}\n")
    wait_until(lambda: linked.request("GET", "/api/state")[1]["stale"])
    fake_control(project, build_delay=2.0)  # wide enough to observe "building" and try a second Stop

    ms, rc, out, err = run_hook(*CASES["stop"], env)
    assert (rc, out, err) == (0, b"", b"")
    assert ms < 500, f"the Stop hook must answer long before the 2 s rebuild finishes, took {ms:.0f} ms"

    # a second Stop while the rebuild is still in flight must not start a second one
    ms2, rc2, out2, err2 = run_hook(*CASES["stop"], env)
    assert (rc2, out2, err2) == (0, b"", b"")
    assert ms2 < 500

    wait_until(lambda: linked.request("GET", "/api/state")[1]["kernel"]["pid"] != old_pid, 10)
    wait_until(lambda: not linked.request("GET", "/api/state")[1]["stale"], 10)
    state = linked.request("GET", "/api/state")[1]
    assert state["scene"]["assembly"] == "demo" and state["scene"]["args"] == shown["args"]
    assert state["kernel"]["generation"] == 2  # not 3: the second Stop found one already in flight
