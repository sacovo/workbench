"""How the hub reaches agents: one interface, a herdr implementation and a null one.

    agents()                               agents the transport sees, with their state
    wake(agent, pointer)                   type a fixed pointer into an idle or done agent, once
    spawn(name, cwd, agent_kind, args)     start an agent in a new pane next to another one
    notify_owner(title, body)              a notice for the owner in the terminal

HerdrTransport speaks herdr's JSON CLI (herdr 0.9.0, measured in spike S2): `agent list`,
`agent get`, `agent prompt --wait --timeout`, `pane split --no-focus`, `agent start` and
`notification show`. It cross-checks herdr's state with `claude agents --json` (idle, busy,
waiting), joined on the Claude session id. NullTransport stands in when herdr is missing: the
hooks still deliver everything, only the idle wake is lost (docs/plan.md, risks).

A wake never types into a blocked agent: the state is read again with `agent get` right before
the prompt, and herdr itself refuses a blocked agent with agent_blocked before sending anything.
herdr's answers to a prompt map to outcomes, and only those that cannot have reached the agent may
be tried again later (policy.py):

    rc 0, agent_prompted              sent
    timeout (after activity was seen)  sent: the turn is still running
    agent_prompt_stalled              stalled: submitted, no activity seen in 5 s; not repeated
    agent_blocked                     refused_blocked: nothing sent
    status not idle or done, or the
      pane hosts another session       skipped: nothing sent
    herdr not installed               unavailable: nothing sent
    anything else                     failed: may have been sent; not repeated
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from workbench.agentlink.texts import POINTER_PREFIX
from workbench.errors import WorkbenchError

STATUSES = ("idle", "working", "blocked", "done", "unknown")
WAKE_STATUSES = ("idle", "done")  # herdr: both mean ready for input
CALL_TIMEOUT = 5.0  # s, for list, get, split and notification calls
WAKE_WAIT_MS = 15000  # herdr waits up to 5 s for activity after submitting, then for the turn to settle
START_TIMEOUT_MS = 60000
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # herdr's rule for agent names


class TransportError(WorkbenchError):
    """The transport cannot do what was asked (herdr missing, failing, or no place for a pane)."""

    http_status = 503


@dataclass(frozen=True)
class AgentInfo:
    pane: str
    status: str  # idle, working, blocked, done or unknown, as herdr reports it
    kind: str | None = None  # "claude"
    name: str | None = None
    session: str | None = None  # Claude session id as herdr knows it (agent_session.value)
    cwd: str | None = None
    seq: int | None = None  # herdr's state_change_seq
    focused: bool | None = None
    claude_status: str | None = None  # idle, busy or waiting from `claude agents --json`

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class WakeResult:
    outcome: str  # sent, stalled, refused_blocked, skipped, unavailable, failed
    status: str | None  # the agent's status as last reported
    detail: str = ""
    duration_s: float = 0.0

    @property
    def sent(self) -> bool:
        """Whether the pointer may have reached the agent (then it is never typed again)."""
        return self.outcome in ("sent", "stalled", "failed")


class AgentTransport(Protocol):
    name: str

    def agents(self) -> list[AgentInfo]: ...

    def wake(self, agent: AgentInfo, pointer: str) -> WakeResult: ...

    def spawn(
        self, name: str, cwd: str | os.PathLike, agent_kind: str, args: Sequence[str] = (), *, near: str | None = None
    ) -> dict: ...

    def notify_owner(self, title: str, body: str) -> bool: ...


class NullTransport:
    """No agent transport: nothing to see, wake or start. The hooks keep working."""

    name = "none"

    def __init__(self, reason: str = "no agent transport configured"):
        self.reason = reason

    def agents(self) -> list[AgentInfo]:
        return []

    def wake(self, agent: AgentInfo, pointer: str) -> WakeResult:
        return WakeResult("unavailable", None, self.reason)

    def spawn(self, name, cwd, agent_kind, args=(), *, near=None) -> dict:
        raise TransportError(f"cannot start an agent: {self.reason}")

    def notify_owner(self, title: str, body: str) -> bool:
        return False


class HerdrError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _agent_info(data: object, claude: dict[str, str] | None) -> AgentInfo | None:
    if not isinstance(data, dict) or not isinstance(data.get("pane_id"), str):
        return None
    status = data.get("agent_status") if data.get("agent_status") in STATUSES else "unknown"
    ref = data.get("agent_session") if isinstance(data.get("agent_session"), dict) else {}
    session = ref.get("value") if ref.get("kind") == "id" and isinstance(ref.get("value"), str) else None
    seq = data.get("state_change_seq")

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    return AgentInfo(
        pane=data["pane_id"],
        status=status,
        kind=text("agent"),
        name=text("name"),
        session=session,
        cwd=text("cwd"),
        seq=seq if type(seq) is int else None,
        focused=data.get("focused") if isinstance(data.get("focused"), bool) else None,
        claude_status=(claude or {}).get(session) if session else None,
    )


class HerdrTransport:
    name = "herdr"

    def __init__(
        self,
        herdr: str = "herdr",
        claude: str | None = "claude",
        *,
        socket_path: str | None = None,
        wake_wait_ms: int = WAKE_WAIT_MS,
        call_timeout: float = CALL_TIMEOUT,
    ):
        self.herdr = herdr
        self.claude = claude
        self.socket_path = socket_path  # HERDR_SOCKET_PATH for the CLI; the agent link fills it from a hook
        self.wake_wait_ms = wake_wait_ms
        self.call_timeout = call_timeout

    # -- the CLI -------------------------------------------------------------------------------

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.socket_path:
            env["HERDR_SOCKET_PATH"] = self.socket_path
        return env

    def _call(self, args: list[str], timeout: float) -> dict:
        """herdr's `result` object; HerdrError with herdr's error code otherwise."""
        try:
            p = subprocess.run(
                [self.herdr, *args], capture_output=True, text=True, timeout=timeout, env=self._env(), stdin=subprocess.DEVNULL
            )
        except FileNotFoundError:
            raise HerdrError("not_installed", f"{self.herdr} not found") from None
        except subprocess.TimeoutExpired:
            raise HerdrError("cli_timeout", f"herdr {' '.join(args[:2])} did not answer within {timeout:.0f} s") from None
        text = p.stdout if p.returncode == 0 else (p.stderr.strip() or p.stdout)
        try:
            data = json.loads(text)
        except ValueError:
            raise HerdrError("cli_error" if p.returncode else "bad_output", text.strip()[:300] or f"exit {p.returncode}") from None
        if not isinstance(data, dict):
            raise HerdrError("bad_output", text.strip()[:300])
        if p.returncode != 0 or "error" in data:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise HerdrError(str(error.get("code") or f"exit_{p.returncode}"), str(error.get("message") or ""))
        result = data.get("result")
        if not isinstance(result, dict):
            raise HerdrError("bad_output", "no result object")
        return result

    def _claude_statuses(self) -> dict[str, str] | None:
        """{session id: idle | busy | waiting} from `claude agents --json`, or None if unavailable."""
        if not self.claude:
            return None
        try:
            p = subprocess.run(
                [self.claude, "agents", "--json"], capture_output=True, text=True, timeout=self.call_timeout, stdin=subprocess.DEVNULL
            )
            data = json.loads(p.stdout) if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if not isinstance(data, list):
            return None
        return {
            a["sessionId"]: a["status"]
            for a in data
            if isinstance(a, dict) and isinstance(a.get("sessionId"), str) and isinstance(a.get("status"), str)
        }

    # -- the interface -------------------------------------------------------------------------

    def agents(self) -> list[AgentInfo]:
        try:
            result = self._call(["agent", "list"], self.call_timeout)
        except HerdrError as exc:
            raise TransportError(f"herdr agent list: {exc}") from None
        listed = result.get("agents") if isinstance(result.get("agents"), list) else []
        claude = self._claude_statuses()
        return [a for a in (_agent_info(d, claude) for d in listed) if a is not None]

    def agent(self, pane: str) -> AgentInfo | None:
        """One agent's state now, or None if the pane hosts no agent."""
        try:
            result = self._call(["agent", "get", pane], self.call_timeout)
        except HerdrError as exc:
            if exc.code in ("agent_not_found", "pane_not_found", "not_found"):
                return None
            raise
        return _agent_info(result.get("agent"), None)

    def wake(self, agent: AgentInfo, pointer: str) -> WakeResult:
        if not pointer.startswith(POINTER_PREFIX) or "\n" in pointer or "\r" in pointer:
            raise ValueError("only a one-line workbench pointer is typed into an agent")
        t0 = time.monotonic()

        def result(outcome: str, status: str | None, detail: str = "") -> WakeResult:
            return WakeResult(outcome, status, detail, round(time.monotonic() - t0, 3))

        try:
            now = self.agent(agent.pane)
        except HerdrError as exc:
            return result("unavailable" if exc.code == "not_installed" else "skipped", None, f"agent get: {exc}")
        if now is None:
            return result("skipped", None, f"no agent in pane {agent.pane} any more")
        if agent.session and now.session and now.session != agent.session:
            return result("skipped", now.status, f"pane {agent.pane} now hosts session {now.session}")
        if now.status not in WAKE_STATUSES:
            return result("skipped", now.status, f"status is {now.status} right before the prompt")
        argv = ["agent", "prompt", agent.pane, pointer, "--wait", "--timeout", str(self.wake_wait_ms)]
        try:
            answer = self._call(argv, self.wake_wait_ms / 1000 + 10)
        except HerdrError as exc:
            if exc.code == "agent_blocked":
                return result("refused_blocked", "blocked", exc.message)
            if exc.code == "agent_prompt_stalled":
                return result("stalled", None, exc.message)
            if exc.code == "timeout":
                return result("sent", "working", f"the turn was still running after {self.wake_wait_ms} ms")
            if exc.code == "not_installed":
                return result("unavailable", None, exc.message)
            if exc.code in ("agent_not_found", "pane_not_found"):
                return result("skipped", None, str(exc))
            return result("failed", None, str(exc))
        after = answer.get("agent") if isinstance(answer.get("agent"), dict) else {}
        status = after.get("agent_status") if isinstance(after.get("agent_status"), str) else None
        return result("sent", status, str(answer.get("type") or ""))

    def spawn(
        self, name: str, cwd: str | os.PathLike, agent_kind: str, args: Sequence[str] = (), *, near: str | None = None
    ) -> dict:
        """Split a pane next to `near` (default: the hub's own herdr pane) without taking focus and
        start an agent there. {"pane", "name", "ready", "status" | "error"}; an agent blocked
        at startup (a trust dialog) comes back ready: false and keeps its pane."""
        if not AGENT_NAME_RE.match(name):
            raise TransportError(f"agent name {name!r}: herdr wants [a-z][a-z0-9_-]{{0,31}}")
        if near is not None:
            where = ["--pane", near]
        elif os.environ.get("HERDR_PANE_ID"):
            where = ["--current"]
        else:
            raise TransportError("no pane to split next to: pass near=<pane id>, or run the hub in a herdr pane")
        try:
            split = self._call(
                ["pane", "split", *where, "--direction", "right", "--cwd", os.fspath(cwd), "--no-focus"], self.call_timeout
            )
            pane = split["pane"]["pane_id"]
        except (HerdrError, KeyError, TypeError) as exc:
            raise TransportError(f"herdr pane split: {exc}") from None
        argv = ["agent", "start", name, "--kind", agent_kind, "--pane", pane, "--timeout", str(START_TIMEOUT_MS)]
        if args:
            argv += ["--", *args]
        try:
            started = self._call(argv, START_TIMEOUT_MS / 1000 + 10)
        except HerdrError as exc:
            if exc.code == "agent_not_ready":
                return {"pane": pane, "name": name, "ready": False, "error": {"code": exc.code, "message": exc.message}}
            raise TransportError(f"herdr agent start in pane {pane}: {exc}") from None
        info = _agent_info(started.get("agent"), None)
        return {"pane": pane, "name": name, "ready": True, "status": info.status if info else None}

    def notify_owner(self, title: str, body: str) -> bool:
        try:
            self._call(["notification", "show", title, "--body", body], self.call_timeout)
        except HerdrError:
            return False
        return True


def make_transport() -> AgentTransport:
    """herdr when it is on PATH, else the null transport."""
    herdr = shutil.which("herdr")
    if herdr is None:
        return NullTransport("herdr is not on PATH")
    return HerdrTransport(herdr, shutil.which("claude"))
