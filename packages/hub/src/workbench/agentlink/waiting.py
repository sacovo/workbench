"""What waits for the agent and for the owner, read from folded item states (review/fold.py).

The hub keeps every item's state in memory (its review watcher refreshes it when review/ changes);
these functions only look at those states, never at the files, so a hook request never waits on
the disk or the project lock.

    unread   an item with owner events after its last delivery (fold: unread_agent). The hooks
             name these; `wbench inbox` returns them and marks them delivered.
    due      an unread item that is the agent's move (status waiting_agent). Only these hold back
             the end of a turn and wake an idle agent; an owner closing an item as done is
             reported, but nobody is woken for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Waiting:
    id: str
    kind: str
    title: str
    status: str
    last: int  # index of the newest unread owner event
    count: int  # unread owner events
    event: str  # type of that newest event: answer, open, reply, status, attach, link, update
    author: str | None  # session id of the agent that opened the item, if an agent did
    by: str = "owner"  # role of the newest unread event: owner, or runner for a job's end

    @property
    def due(self) -> bool:
        return self.status == "waiting_agent"

    def news(self) -> str:
        """What happened, in two or three fixed words: "owner answered", "job ended"."""
        from workbench.agentlink.texts import verb

        return "job ended" if self.by == "runner" else f"owner {verb(self.event)}"


def event_type(state: dict, index: int) -> str:
    """The type of event `index` as far as a folded state tells it."""
    if any(a.get("event") == index for a in state.get("answers", {}).values()):
        return "answer"
    if index == 0:
        return "open"
    last = state.get("last_activity") or {}
    if last.get("index") == index and isinstance(last.get("event"), str):
        return last["event"]
    return "update"


def _author(state: dict) -> str | None:
    by = (state.get("opened") or {}).get("by") or {}
    session = by.get("session")
    return session if by.get("role") == "agent" and isinstance(session, str) else None


def waiting(items: Iterable[dict]) -> list[Waiting]:
    """Items with unread owner events, in the order given (the store's id order)."""
    out = []
    for state in items:
        unread = state.get("unread_agent") or []
        if not unread:
            continue
        last = max(unread)
        out.append(
            Waiting(
                id=state["id"],
                kind=state["kind"],
                title=state["title"],
                status=state["status"],
                last=last,
                count=len(unread),
                event=event_type(state, last),
                author=_author(state),
                by="runner" if (state.get("last_activity") or {}).get("role") == "runner" and last == state["last_activity"]["index"] else "owner",
            )
        )
    return out


def owner_moves(items: Iterable[dict]) -> dict[str, int]:
    """{item id: index of the newest event the owner has not seen} for items that are the owner's
    move with agent or reviewer events after the owner's last one (a question, a reply)."""
    out = {}
    for state in items:
        unread = state.get("unread_owner") or []
        if unread and state.get("status") == "waiting_owner":
            out[state["id"]] = max(unread)
    return out
