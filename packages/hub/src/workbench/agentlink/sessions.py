"""A Claude Code session as the hub knows it from its hooks.

Every hook request carries the session id, the herdr pane (HERDR_PANE_ID, if the agent runs in
herdr) and the working directory, so a hub that restarts learns a running agent again from its
next hook call; SessionStart adds how the session started. `announced` remembers, per item, the
newest owner event a context injection has named to this session, so PostToolUse names each event
once instead of after every tool call.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PANE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}$")
ITEM_ID_RE = re.compile(r"^R-\d{4,}$")


@dataclass
class Session:
    session: str
    pane: str | None = None
    cwd: str | None = None
    source: str | None = None  # startup, resume, clear, compact or fork (SessionStart)
    agent_type: str | None = None  # set when the session runs `claude --agent <name>`
    herdr_socket: str | None = None
    registered_at: float = 0.0
    last_seen: float = 0.0
    last_event: str = ""
    turn: str = "unknown"  # working, continuing (Stop held back), ended, as the hooks saw it
    turns: int = 0  # prompts since the conversation started; 0: still waiting for the first one
    announced: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> Session | None:
        """A session from saved state, or None if the entry is not one."""
        if not isinstance(data, dict) or not isinstance(data.get("session"), str):
            return None
        if not SESSION_RE.match(data["session"]):
            return None
        s = cls(session=data["session"])
        for key in ("pane", "cwd", "source", "agent_type", "herdr_socket"):
            value = data.get(key)
            if value is None or isinstance(value, str):
                setattr(s, key, value)
        if s.pane is not None and not PANE_RE.match(s.pane):
            s.pane = None
        for key in ("registered_at", "last_seen"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(s, key, float(value))
        for key in ("last_event", "turn"):
            if isinstance(data.get(key), str):
                setattr(s, key, data[key])
        if type(data.get("turns")) is int:
            s.turns = data["turns"]
        announced = data.get("announced")
        if isinstance(announced, dict):
            s.announced = {k: v for k, v in announced.items() if ITEM_ID_RE.match(str(k)) and type(v) is int}
        return s
