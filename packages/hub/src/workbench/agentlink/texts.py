"""The only texts the agent link puts in front of the agent: fixed templates, never item content.

    wake pointer, typed into an idle agent by the transport (herdr):
        Workbench: owner answered R-0042; owner opened R-0043. Run: wbench inbox
    Stop hook reason, when the end of a turn is held back once:
        Workbench: unread owner events on R-0042, R-0043. Run: wbench inbox
    context from the UserPromptSubmit and PostToolUse hooks:
        Workbench: 2 items have owner events the agent has not read: R-0042 decision "Front
        limit" (owner answered); R-0043 comment "Edge too thin" (owner opened). Run: wbench inbox

The pointer and the reason carry item ids and a verb from a fixed list; the context adds kinds,
titles and a count. None of them carries a body, an answer, a question or an option. The agent
pulls the content with `wbench inbox`, which records the delivery, so every owner event reaches it
once, as data with its author (spike S2 found double delivery when hooks injected content and a
pointer was typed as well).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from workbench.agentlink.waiting import Waiting

POINTER_PREFIX = "Workbench:"  # the hook script recognises a typed pointer by it
PULL = "Run: wbench inbox"
MAX_LISTED = 5  # items named in one text; the rest are counted
MAX_TITLE = 80  # characters of a title in the context

VERBS = {
    "answer": "answered",
    "open": "opened",
    "reply": "replied to",
    "status": "set the status of",
    "attach": "attached a file to",
    "link": "linked",
}


def verb(event: str) -> str:
    return VERBS.get(event, "updated")


def clean_title(title: object) -> str:
    """One line, printable, no double quotes, at most MAX_TITLE characters."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(title)).split())
    text = text.replace('"', "'")
    if len(text) > MAX_TITLE:
        text = text[: MAX_TITLE - 1].rstrip() + "…"
    return text


def _more(items: Sequence) -> str:
    extra = len(items) - MAX_LISTED
    return f" and {extra} more" if extra > 0 else ""


def wake_pointer(due: Sequence[Waiting]) -> str:
    """Typed into an idle agent: ids and fixed verbs only."""
    if not due:
        raise ValueError("a wake pointer needs at least one item")
    parts = "; ".join(f"owner {verb(w.event)} {w.id}" for w in due[:MAX_LISTED])
    return f"{POINTER_PREFIX} {parts}{_more(due)}. {PULL}"


def stop_reason(due: Sequence[Waiting]) -> str:
    """The Stop hook's reason for holding back the end of a turn once: ids only."""
    if not due:
        raise ValueError("a stop reason needs at least one item")
    ids = ", ".join(w.id for w in due[:MAX_LISTED])
    return f"{POINTER_PREFIX} unread owner events on {ids}{_more(due)}. {PULL}"


def context(waiting: Sequence[Waiting]) -> str:
    """additionalContext for UserPromptSubmit and PostToolUse: count, ids, kinds and titles."""
    if not waiting:
        raise ValueError("context needs at least one item")
    n = len(waiting)
    listed = "; ".join(
        f'{w.id} {w.kind} "{clean_title(w.title)}" (owner {verb(w.event)})' for w in waiting[:MAX_LISTED]
    )
    noun = "item has" if n == 1 else "items have"
    return f"{POINTER_PREFIX} {n} {noun} owner events the agent has not read: {listed}{_more(waiting)}. {PULL}"
