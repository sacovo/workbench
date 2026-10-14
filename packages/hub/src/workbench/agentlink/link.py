"""The hub's side of the agent link (docs/plan.md, "Agent messages"; spike S2).

Hooks. plugin/hooks/wb_hook.py posts the SessionStart, UserPromptSubmit, PostToolUse and Stop
hooks of every Claude Code session in this project to POST /api/hooks/<event> (routes.py).
handle_hook() answers from memory, on the event loop, without touching herdr or the disk:

    session-start        registers the session: id, herdr pane, cwd, how it started
    user-prompt-submit   context naming the items with unread owner events; none when the prompt
                         is itself a typed pointer
    post-tool-use        the same context, once per owner event; nothing right after `wb inbox`
    stop                 holds the end of the turn back once, with the fixed reason, when owner
                         events on items that are the agent's move are unread and
                         stop_hook_active is the JSON boolean false; otherwise the turn has
                         ended, and turn_ended() (below) is called for a registered session

Wake. tick() runs on the link's own thread, once a second and right after hooks and review
changes. It applies the wake policy (policy.py) through the transport, and tells the owner through
the transport when the agent is blocked while owner events wait and when a decision or a
measurement request becomes the owner's move.

Records. Wake attempts, owner notices and registrations are kept in memory (snapshot(), GET
/api/agents) and appended to .workbench/logs/agentlink.jsonl; the sessions and what has been
pointed at survive a hub restart in .workbench/agentlink.json. Nothing here writes to review/:
a delivery is recorded only by `wb inbox`.

What the hub provides, AgentLink(project, review_items, transport, token=, poke_review=, turn_ended=):
    project        the Project (for .workbench/)
    review_items   () -> the folded state of every item from the hub's memory, which its review
                   watcher keeps current; called on the event loop, so it must not read files
    transport      make_transport(): herdr, or the null transport when herdr is missing
    token          the hub's token; the routes check it as well as the hub's Guard does
    poke_review    optional () -> None: refresh that memory now (called right after `wb inbox`)
    turn_ended     optional () -> None: a registered session's Stop hook just ended its turn
                   (not held back). The hub rebuilds the shown assembly if its code is stale
                   (Hub._agent_turn_ended); called on the event loop, so it must not block.
It calls start() once the hub runs, stop() when it stops (blocks up to 2 s; run it off the loop),
and review_changed() whenever its review snapshot changes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from typing import Callable, Iterable

from workbench.agentlink import policy
from workbench.agentlink.sessions import ITEM_ID_RE, PANE_RE, SESSION_RE, Session
from workbench.agentlink.texts import MAX_LISTED, clean_title, context, stop_reason, wake_pointer
from workbench.agentlink.transport import AgentInfo, AgentTransport, NullTransport, TransportError
from workbench.agentlink.waiting import Waiting, owner_moves, waiting
from workbench.errors import InvalidInput, NotFound
from workbench.project import Project, atomic_write

log = logging.getLogger("workbench.agentlink")

HOOK_EVENTS = ("session-start", "user-prompt-submit", "post-tool-use", "stop")
STATE_FILE = "agentlink.json"  # in .workbench/
LOG_FILE = "agentlink.jsonl"  # in .workbench/logs/

TICK = 1.0  # s between ticks when nothing kicks the link
POLL_SOON = 0.4  # s after a Stop that ended a turn: herdr reported done 0.17-0.34 s after it (S2)
POLL_BURST = 0.5  # s between looks while BURST lasts after such a Stop
BURST = 5.0
POLL_BLOCKED = 2.0  # s between looks while the agent is blocked, or a wake waits for a new state
POLL_WORKING = 10.0  # s while it works (its Stop hook makes the link look sooner; this catches Esc)
POLL_UNAVAILABLE = 30.0
MAX_ATTEMPTS = 3  # refused or skipped wakes per owner event before the link gives up on it
MAX_SESSIONS = 20
KEEP_HISTORY = 50
OWNER_NOTICE_KINDS = ("decision", "measurement")  # owner's move that asks something of the owner
STOP_TIMEOUT = 2.0  # s stop() waits for a tick in progress

_FIELDS = {
    "session-start": ("source", "agent_type", "herdr_socket"),
    "user-prompt-submit": ("pointer_prompt",),
    "post-tool-use": ("tool", "pulled"),
    "stop": ("stop_hook_active",),
}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(payload: dict, key: str, limit: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value or "\0" in value:
        raise InvalidInput(f"hook field {key}: expected a single-line string of at most {limit} characters")
    return value


def check_payload(event: str, payload: object) -> dict:
    """The hook script's summary, checked strictly; every field present (None when not sent)."""
    if not isinstance(payload, dict):
        raise InvalidInput("the hook body must be a JSON object")
    allowed = ("session", "pane", "cwd", *_FIELDS[event])
    unknown = sorted(str(k) for k in payload if k not in allowed)
    if unknown:
        raise InvalidInput(f"unknown hook field(s) {', '.join(unknown)} for {event}; allowed: {', '.join(allowed)}")
    session = payload.get("session")
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise InvalidInput("hook field session: expected a Claude Code session id")
    pane = payload.get("pane")
    if pane is not None and (not isinstance(pane, str) or not PANE_RE.match(pane)):
        raise InvalidInput("hook field pane: expected a herdr pane id like w1:p2")
    out: dict = {"session": session, "pane": pane, "cwd": _text(payload, "cwd", 4096)}
    if event == "session-start":
        out["source"] = _text(payload, "source", 32)
        out["agent_type"] = _text(payload, "agent_type", 256)
        out["herdr_socket"] = _text(payload, "herdr_socket", 4096)
        if out["herdr_socket"] is not None and not out["herdr_socket"].startswith("/"):
            raise InvalidInput("hook field herdr_socket: expected an absolute path")
    elif event == "post-tool-use":
        out["tool"] = _text(payload, "tool", 128)
    for key in ("pointer_prompt", "pulled"):
        if key in _FIELDS[event]:
            value = payload.get(key, False)
            if type(value) is not bool:
                raise InvalidInput(f"hook field {key}: expected true or false")
            out[key] = value
    if event == "stop":
        out["stop_hook_active"] = payload.get("stop_hook_active")  # kept as sent; only `is False` blocks
    return out


def _context_output(hook_event_name: str, unread: list[Waiting]) -> dict:
    return {"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": context(unread)}}


class AgentLink:
    def __init__(
        self,
        project: Project,
        review_items: Callable[[], Iterable[dict]],
        transport: AgentTransport | None = None,
        *,
        token: str | None = None,
        poke_review: Callable[[], None] | None = None,
        turn_ended: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        persist: bool = True,
    ):
        self.project = project
        self._review_items = review_items
        self.transport: AgentTransport = transport if transport is not None else NullTransport()
        self.token = token
        self._poke_review = poke_review
        self.turn_ended = turn_ended
        self.clock = clock
        self.persist = persist
        self._lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.pointed: dict[str, int] = {}  # item -> newest owner event a Stop block or a wake named
        self.owner_seen: dict[str, int] | None = None  # item -> newest event the owner was told of
        self.agents: dict[str, AgentInfo] = {}  # pane -> state at the last look
        self.agents_at: float | None = None
        self.history: deque[dict] = deque(maxlen=KEEP_HISTORY)
        self._tries: dict[tuple[str, int], int] = {}
        self._held: dict[str, tuple] = {}  # pane -> (status, seq) a wake was refused or skipped in
        self._blocked_noted: set[str] = set()
        self._due_keys: set[tuple[str, int]] = set()
        self._last_wait: tuple | None = None
        self._poll_at = 0.0
        self._burst_until = 0.0
        self._log_queue: list[dict] = []
        self._dirty = False
        self._kick = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        if persist:
            self._load()

    # -- hooks ---------------------------------------------------------------------------------

    def handle_hook(self, event: str, payload: object) -> dict | None:
        """The output for the hook script to print, or None. Raises NotFound for an unknown
        event and InvalidInput for a malformed body; never blocks on herdr or the disk."""
        if event not in HOOK_EVENTS:
            raise NotFound(f"no hook event {event!r}; known: {', '.join(HOOK_EVENTS)}")
        data = check_payload(event, payload)
        now = self.clock()
        items = self._items()
        poke = kick = ended = False
        out = None
        with self._lock:
            s = self._register(data, event, now)
            unread = waiting(items) if items is not None else None
            if event == "session-start":
                s.announced.clear()  # a new, cleared, resumed or compacted context: name everything again
                if data["source"] == "compact":
                    s.turns = max(s.turns, 1)  # compaction happens inside a conversation
                else:
                    s.turns = 0  # the owner is about to write the next prompt: do not type into it
                kick = True
            elif event == "user-prompt-submit":
                s.turn, s.turns = "working", s.turns + 1
                if unread and not data["pointer_prompt"]:
                    out = _context_output("UserPromptSubmit", unread)
                self._announce(s, unread)
            elif event == "post-tool-use":
                s.turn = "working"
                if data["pulled"]:
                    poke = True  # the snapshot is stale until the review watcher sees `wb inbox`'s writes
                    self._announce(s, unread)
                elif unread and any(w.last > s.announced.get(w.id, -1) for w in unread):
                    out = _context_output("PostToolUse", unread)
                    self._announce(s, unread)
            else:
                due = [w for w in unread or () if w.due]
                if data["stop_hook_active"] is False and due:
                    out = {"decision": "block", "reason": stop_reason(due)}
                    self._announce(s, unread)
                    self._point(due)
                    s.turn = "continuing"
                else:
                    s.turn = "ended"
                    self._poll_at = now + POLL_SOON
                    self._burst_until = now + BURST
                    kick = ended = True
        if poke and self._poke_review is not None:
            with suppress(Exception):
                self._poke_review()
        if ended and self.turn_ended is not None:
            with suppress(Exception):
                self.turn_ended()
        if kick:
            self.kick()
        return out

    def _register(self, data: dict, event: str, now: float) -> Session:
        sid = data["session"]
        s = self.sessions.get(sid)
        changed = s is None
        if s is None:
            # first seen through another hook: a hub restart in the middle of a conversation
            s = Session(session=sid, registered_at=now, turns=0 if event == "session-start" else 1)
            self.sessions[sid] = s
        pane = data["pane"]
        if pane and s.pane != pane:
            for other in [o for o in self.sessions.values() if o.pane == pane and o is not s]:
                del self.sessions[other.session]  # a pane hosts one conversation at a time
            s.pane = pane
            changed = True
        if data["cwd"]:
            s.cwd = data["cwd"]
        if event == "session-start":
            s.source, s.agent_type = data["source"], data["agent_type"]
            if data["herdr_socket"]:
                s.herdr_socket = data["herdr_socket"]
                if getattr(self.transport, "socket_path", "") is None:
                    self.transport.socket_path = data["herdr_socket"]  # type: ignore[attr-defined]
            changed = True
        s.last_seen, s.last_event = now, event
        if changed:
            self._dirty = True
            self._record({"what": "session", "event": event, "session": sid, "pane": s.pane, "cwd": s.cwd, "source": s.source}, now)
        if len(self.sessions) > MAX_SESSIONS:
            for old in sorted(self.sessions.values(), key=lambda o: o.last_seen)[: len(self.sessions) - MAX_SESSIONS]:
                del self.sessions[old.session]
        return s

    def _announce(self, s: Session, unread: list[Waiting] | None) -> None:
        if unread is not None:
            s.announced = {w.id: max(s.announced.get(w.id, -1), w.last) for w in unread}

    def _point(self, due: Iterable[Waiting]) -> None:
        for w in due:
            if w.last > self.pointed.get(w.id, -1):
                self.pointed[w.id] = w.last
                self._dirty = True

    # -- the wake policy -----------------------------------------------------------------------

    def tick(self) -> list[dict]:
        """One round: owner notices, then the wake policy. Calls the transport; run it off the
        event loop (the link's thread does). Returns what it recorded."""
        now = self.clock()
        items = self._items()
        if items is None:
            return []
        done: list[dict] = []
        with self._lock:
            unread = waiting(items)
            due = [w for w in unread if w.due and w.last > self.pointed.get(w.id, -1)]
            keys = {(w.id, w.last) for w in due}
            if not keys <= self._due_keys:
                self._poll_at = now  # a new owner event to wake for: look now
            self._due_keys = keys
            self._tries = {k: n for k, n in self._tries.items() if k in keys}
            news = self._owner_news(items)
            look = bool(due) and now >= self._poll_at
        if news:
            done.append(self._notify_owner_news(news, items, now))
        if look:
            done.extend(self._wake_round(due, now))
        self._flush()
        return done

    def _wake_round(self, due: list[Waiting], now: float) -> list[dict]:
        ids = [w.id for w in due]
        try:
            agents = self.transport.agents()
        except TransportError as exc:
            with self._lock:
                self._poll_at = now + POLL_UNAVAILABLE
                return self._waiting_record("unavailable", None, None, ids, now, detail=str(exc))
        with self._lock:
            self.agents, self.agents_at = {a.pane: a for a in agents}, now
            self._blocked_noted &= {a.pane for a in agents if a.status == "blocked" or a.claude_status == "waiting"}
            choice = policy.choose(due, list(self.sessions.values()), agents)
            if choice is None:
                self._poll_at = now + POLL_WORKING
                return self._waiting_record("no_agent", None, None, ids, now)
            session, agent = choice
            verdict = policy.judge(session, agent, self._held.get(agent.pane))
            if verdict != policy.WAKE:
                if verdict in ("working", "busy"):
                    self._poll_at = now + (POLL_BURST if now < self._burst_until else POLL_WORKING)
                elif verdict in ("blocked", "held"):
                    self._poll_at = now + POLL_BLOCKED
                else:
                    self._poll_at = now + POLL_WORKING
                notice = verdict == "blocked" and agent.pane not in self._blocked_noted
                if notice:
                    self._blocked_noted.add(agent.pane)
                records = self._waiting_record(verdict, session, agent, ids, now)
            else:
                spent = [w for w in due if self._tries.get((w.id, w.last), 0) >= MAX_ATTEMPTS]
                records = []
                if spent:
                    self._point(spent)
                    records.append(self._record({"what": "wake", "outcome": "gave_up", "items": [w.id for w in spent],
                                                 "detail": f"{MAX_ATTEMPTS} wakes refused or skipped"}, now))
                    due = [w for w in due if w not in spent]
                if not due:
                    return records
                for w in due:
                    self._tries[(w.id, w.last)] = self._tries.get((w.id, w.last), 0) + 1
                pointer = wake_pointer(due)
        if verdict != policy.WAKE:
            if notice:
                listed = ", ".join(ids[:MAX_LISTED]) + (f" and {len(ids) - MAX_LISTED} more" if len(ids) > MAX_LISTED else "")
                sent = self.transport.notify_owner(
                    "Workbench: the agent waits at a prompt",
                    f"Pane {agent.pane}: owner events on {listed} reach it once it continues.",
                )
                with self._lock:
                    records.append(self._record({"what": "owner_notice", "reason": "blocked", "pane": agent.pane, "items": ids, "sent": sent}, now))
            return records
        result = self.transport.wake(agent, pointer)
        with self._lock:
            if result.sent:
                self._point(due)
                self._held.pop(agent.pane, None)
                self._poll_at = now + POLL_BLOCKED
            elif result.outcome == "unavailable":
                self._poll_at = now + POLL_UNAVAILABLE
            else:  # refused_blocked, skipped: nothing reached the agent; wait for another state
                self._held[agent.pane] = (agent.status, agent.seq)
                self._poll_at = now + POLL_BLOCKED
            self._last_wait = None
            records.append(
                self._record(
                    {
                        "what": "wake",
                        "outcome": result.outcome,
                        "session": session.session,
                        "pane": agent.pane,
                        "status_before": agent.status,
                        "status_after": result.status,
                        "items": {w.id: w.last for w in due},
                        "pointer": pointer,
                        "detail": result.detail,
                        "duration_s": result.duration_s,
                    },
                    now,
                )
            )
        return records

    def _waiting_record(self, verdict, session, agent, ids, now, detail: str = "") -> list[dict]:
        """A record of why nobody was woken, written only when the reason changes."""
        key = (verdict, agent.pane if agent else None, agent.status if agent else None, tuple(ids))
        if key == self._last_wait:
            return []
        self._last_wait = key
        rec = {"what": "wake", "outcome": "waiting", "verdict": verdict, "items": ids}
        if session is not None:
            rec.update(session=session.session, pane=agent.pane, status=agent.status, claude_status=agent.claude_status)
        if detail:
            rec["detail"] = detail
        return [self._record(rec, now)]

    def _owner_news(self, items: list[dict]) -> list[str]:
        kinds = {s.get("id"): s.get("kind") for s in items}
        moves = {i: last for i, last in owner_moves(items).items() if kinds.get(i) in OWNER_NOTICE_KINDS}
        if self.owner_seen is None:  # first look, nothing saved: what waits already is not news
            self.owner_seen = moves
            self._dirty = True
            return []
        news = sorted(i for i, last in moves.items() if last > self.owner_seen.get(i, -1))
        for i in news:
            self.owner_seen[i] = moves[i]
        if news:
            self._dirty = True
        return news

    def _notify_owner_news(self, news: list[str], items: list[dict], now: float) -> dict:
        states = {s["id"]: s for s in items}
        title = f"Workbench: {news[0]} waits for you" if len(news) == 1 else f"Workbench: {len(news)} items wait for you"
        body = "; ".join(f'{i} {states[i]["kind"]} "{clean_title(states[i]["title"])}"' for i in news[:MAX_LISTED])
        if len(news) > MAX_LISTED:
            body += f"; and {len(news) - MAX_LISTED} more"
        sent = self.transport.notify_owner(title, body)
        with self._lock:
            return self._record({"what": "owner_notice", "reason": "owner_move", "items": news, "sent": sent}, now)

    # -- thread --------------------------------------------------------------------------------

    def start(self) -> None:
        """Run tick() on the link's own daemon thread until stop()."""
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="agentlink", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._stopping.set()
            self._kick.set()
            thread.join(timeout)
        self._flush()

    def kick(self) -> None:
        """Look now (thread-safe, never blocks)."""
        self._kick.set()

    def review_changed(self) -> None:
        self.kick()

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._kick.wait(self._delay())
            self._kick.clear()
            if self._stopping.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bug; keep the link alive and the traceback in the log
                log.exception("agent link: tick failed")

    def _delay(self) -> float:
        with self._lock:
            wait = self._poll_at - self.clock()
        return TICK if wait <= 0 else max(0.05, min(TICK, wait))

    # -- records and state ---------------------------------------------------------------------

    def _items(self) -> list[dict] | None:
        try:
            return list(self._review_items())
        except Exception:  # noqa: BLE001 - the hub's state; the hooks fail open
            log.exception("agent link: reading the hub's review state failed")
            return None

    def _record(self, rec: dict, now: float) -> dict:
        rec = {"at": _iso(now), **rec}
        self.history.append(rec)
        self._log_queue.append(rec)
        return rec

    def _state(self) -> dict:
        return {
            "version": 1,
            "sessions": [s.to_dict() for s in self.sessions.values()],
            "pointed": dict(self.pointed),
            "owner_seen": None if self.owner_seen is None else dict(self.owner_seen),
        }

    def _flush(self) -> None:
        with self._lock:
            lines, self._log_queue = self._log_queue, []
            state = self._state() if self._dirty else None
            self._dirty = False
        if not self.persist or (not lines and state is None):
            return
        try:
            if lines:
                with open(self.project.ensure_logs_dir() / LOG_FILE, "a", encoding="utf-8") as f:
                    f.write("".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in lines))
            if state is not None:
                atomic_write(self.project.ensure_state_dir() / STATE_FILE, json.dumps(state, indent=1) + "\n")
        except OSError as exc:
            log.warning("agent link: cannot write .workbench/: %s", exc)
            with self._lock:
                self._dirty = self._dirty or state is not None

    def _load(self) -> None:
        path = self.project.state_dir / STATE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("agent link: ignoring %s: %s", path, exc)
            return
        if not isinstance(data, dict):
            return
        for entry in data.get("sessions") or []:
            s = Session.from_dict(entry)
            if s is not None:
                self.sessions[s.session] = s

        def marks(value) -> dict[str, int]:
            if not isinstance(value, dict):
                return {}
            return {k: v for k, v in value.items() if ITEM_ID_RE.match(str(k)) and type(v) is int}

        self.pointed = marks(data.get("pointed"))
        if isinstance(data.get("owner_seen"), dict):
            self.owner_seen = marks(data["owner_seen"])

    def snapshot(self) -> dict:
        """For GET /api/agents: sessions with herdr's state at the last look, what waits for the
        agent and the latest records. From memory only."""
        items = self._items() or []
        with self._lock:
            unread = waiting(items)
            sessions = sorted(self.sessions.values(), key=lambda s: s.last_seen, reverse=True)
            return {
                "transport": self.transport.name,
                "agents_checked_at": _iso(self.agents_at) if self.agents_at else None,
                "sessions": [
                    {
                        **{k: v for k, v in s.to_dict().items() if k != "announced"},
                        "registered_at": _iso(s.registered_at),
                        "last_seen": _iso(s.last_seen),
                        "agent": self.agents[s.pane].to_dict() if s.pane in self.agents else None,
                    }
                    for s in sessions
                ],
                "waiting": [
                    {
                        "id": w.id,
                        "kind": w.kind,
                        "title": w.title,
                        "status": w.status,
                        "event": w.event,
                        "unread": w.count,
                        "due": w.due,
                        "pointed": w.last <= self.pointed.get(w.id, -1),
                    }
                    for w in unread
                ],
                "history": list(self.history)[-20:],
            }
