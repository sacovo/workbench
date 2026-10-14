"""The agent link mounted in the real hub: create_app(hub, extra_routes=routes(link)) behind its
Guard (Host, Origin, token), with the fake kernel, the link reading the hub's own in-memory review
state, and the plugin's hook script calling it as Claude Code would. This is the wiring that
server.run() needs (see workbench.agentlink.link, "What the hub provides")."""

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
        sock = bind(0)
        self.port = sock.getsockname()[1]
        command = [sys.executable, str(FAKE_KERNEL), "serve", "--project", str(project.root)]
        self.hub = Hub(project, self.token, self.port, kernel_command=command, auto_show=False, review_poll=0.1)
        self.link = AgentLink(project, lambda: self.hub.review.get("items", []), NullTransport(), token=self.token,
                              poke_review=self.hub.poke_review)

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
                     headers={"X-Workbench-Token": self.token, "Content-Type": "application/json", **(headers or {})})
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
        "decision": "block", "reason": "Workbench: unread owner events on R-0001. Run: wb inbox"}
    assert run_hook("Stop", STOP_ACTIVE, env)[1:] == (0, b"", b"")

    # the agent pulls: `wb inbox` (here through the hub's op route) records the delivery
    status, inbox = linked.request("POST", "/api/op/inbox", {"session": SID})
    assert status == 200 and [i["id"] for i in inbox["items"]] == ["R-0001"]
    wait_until(lambda: not linked.items()[0]["unread_agent"])
    assert run_hook(*CASES["stop"], env)[1:] == (0, b"", b"")

    status, snap = linked.request("GET", "/api/agents")
    assert status == 200 and snap["sessions"][0]["session"] == SID and snap["waiting"] == []
    assert linked.request("GET", "/api/agents", headers={"X-Workbench-Token": "wrong"})[0] == 401
    assert linked.request("GET", "/api/agents", headers={"Origin": "http://evil.example"})[0] == 403
    assert linked.request("GET", "/api/agents", headers={"Host": f"evil.example:{linked.port}"})[0] == 403
