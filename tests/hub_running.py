"""A real hub (Starlette under uvicorn, on 127.0.0.1 and an ephemeral port) in a thread of the test
process, with the fake kernel (fake_kernel.py) or another kernel command, and a page client that
speaks the page's WebSocket protocol. Nothing here is mocked: HTTP and WebSocket go over sockets."""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from workbench.hub.app import create_app
from workbench.hub.hub import Hub
from workbench.hub.server import bind
from workbench.project import Project

FAKE_KERNEL = Path(__file__).with_name("fake_kernel.py")
DEFAULT = object()


def wait_until(predicate, timeout: float = 10.0, what: str = "condition", interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        time.sleep(interval)


def kernel_processes(root: Path) -> list[int]:
    """Live processes whose command line names a kernel serving `root` (fake or real)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [w.decode(errors="replace") for w in cmdline]
        if any(w.endswith(("fake_kernel.py", "workbench_kernel")) for w in words) and str(root) in words:
            found.append(int(entry.name))
    return found


def fake_control(project: Project, **behaviour) -> None:
    """What the fake kernel does on its next requests (fake_kernel.py, docstring)."""
    project.ensure_state_dir()
    (project.state_dir / "fake-kernel.json").write_text(json.dumps(behaviour))


class RunningHub:
    def __init__(self, project: Project, *, kernel_command: list[str] | None = None, auto_show: bool = False,
                 retire_grace: float = 30.0, static_dir: Path | None = None, ready_timeout: float = 20.0):
        self.project = project
        self.token = secrets.token_urlsafe(16)
        sock = bind(0)
        self.port = sock.getsockname()[1]
        command = kernel_command or [sys.executable, str(FAKE_KERNEL), "serve", "--project", str(project.root)]
        self.hub = Hub(project, self.token, self.port, kernel_command=command, auto_show=auto_show,
                       review_poll=0.1, code_poll=0.2, retire_grace=retire_grace)
        app = create_app(self.hub, **({} if static_dir is None else {"static_dir": static_dir}))
        config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on", ws_ping_interval=None)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        wait_until(lambda: self.server.started, 20, "the server to start")
        self.wait_state(lambda s: s["kernel"]["state"] in ("ready", "crashed"), ready_timeout, "the first kernel")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(30)
        assert not self.thread.is_alive(), "the hub did not stop"

    def request(self, method: str, path: str, body=None, *, token=DEFAULT, headers: dict | None = None):
        """(status, parsed JSON or raw bytes)."""
        h = {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if token is DEFAULT:
            token = self.token
        if token is not None:
            h["X-Workbench-Token"] = token
        h.update(headers or {})
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as response:
                status, raw, ctype = response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, raw, ctype = exc.code, exc.read(), exc.headers.get("Content-Type", "")
        return status, (json.loads(raw) if "json" in ctype else raw)

    def op(self, name: str, **arguments) -> dict:
        status, body = self.request("POST", f"/api/op/{name}", arguments)
        assert status == 200, (status, body)
        return body

    def state(self) -> dict:
        status, body = self.request("GET", "/api/state")
        assert status == 200, body
        return body

    def wait_state(self, predicate, timeout: float = 10.0, what: str = "state") -> dict:
        return wait_until(lambda: (lambda s: s if predicate(s) else None)(self.state()), timeout, what)

    def page(self, **kwargs) -> PageClient:
        return PageClient(self, **kwargs)


class PageClient:
    """The page's side of /ws: every message kept in order, waited for by type and predicate."""

    def __init__(self, hub: RunningHub, origin: str | None = None, token: str | None = None):
        uri = f"ws://127.0.0.1:{hub.port}/ws?token={hub.token if token is None else token}"
        self.ws = connect(uri, origin=origin, max_size=None, open_timeout=10).__enter__()  # closed in close()
        self.messages: list[dict] = []
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            for text in self.ws:
                with self._cond:
                    self.messages.append(json.loads(text))
                    self._cond.notify_all()
        except ConnectionClosed:
            pass

    def wait(self, kind: str, predicate=lambda m: True, timeout: float = 10.0, after: int = 0) -> dict:
        """The first message of type `kind` at index >= `after` that satisfies `predicate`."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.messages[after:]:
                    if m.get("type") == kind and predicate(m):
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    seen = [m.get("type") for m in self.messages[after:]]
                    raise AssertionError(f"no {kind} message within {timeout} s; got {seen}")
                self._cond.wait(left)

    def types(self) -> list[str]:
        with self._cond:
            return [m.get("type") for m in self.messages]

    def mark(self) -> int:
        with self._cond:
            return len(self.messages)

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps(message))

    def show_scene(self, scene: str) -> None:
        """What the page does after it drew a scene."""
        self.send({"type": "shown", "scene": scene})

    def close(self) -> None:
        self.ws.close()
