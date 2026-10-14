"""The wake policy (docs/plan.md, "Agent messages"; spike S2): who gets woken, and when not.

When an owner event makes an item the agent's move and nothing has pointed at that event yet (no
Stop hook held a turn back for it, no earlier wake), one registered agent of this project is woken
once with the fixed pointer, and only when herdr reports it idle or done:

    working          left alone: its PostToolUse and Stop hooks deliver, and the Stop that ends
                     its turn makes the hub look again within a second
    blocked          never typed into (a permission prompt or a question); looked at again on
                     herdr's next state change; the owner is told once
    busy / waiting   `claude agents --json` disagrees with herdr: treated as working / blocked
    held             a wake was refused or skipped in this very state (same status and
                     state_change_seq): wait until herdr reports a different one
    first prompt     a conversation that has not had its first prompt is not typed into: the
                     owner is about to write it, and the prompt hook will point at the inbox

The agent chosen is a registered session whose herdr pane hosts it (same session id when herdr
knows one): the session that opened a due item if it is there, else the one seen most recently.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from workbench.agentlink.sessions import Session
from workbench.agentlink.transport import WAKE_STATUSES, AgentInfo
from workbench.agentlink.waiting import Waiting

WAKE = "wake"


def candidates(sessions: Iterable[Session], agents: Sequence[AgentInfo]) -> list[tuple[Session, AgentInfo]]:
    """Registered sessions paired with the agent herdr reports in their pane."""
    by_pane = {a.pane: a for a in agents}
    out = []
    for s in sessions:
        agent = by_pane.get(s.pane) if s.pane else None
        if agent is None or agent.kind != "claude":
            continue  # only a pane herdr recognises as Claude Code is ever typed into
        if agent.session is not None and agent.session != s.session:
            continue  # the pane hosts another conversation now
        out.append((s, agent))
    return out


def choose(due: Sequence[Waiting], sessions: Iterable[Session], agents: Sequence[AgentInfo]) -> tuple[Session, AgentInfo] | None:
    pairs = candidates(sessions, agents)
    if not pairs:
        return None
    authors = {w.author for w in due if w.author}
    return max(pairs, key=lambda pair: (pair[0].session in authors, pair[0].last_seen))


def judge(session: Session, agent: AgentInfo, held: tuple | None) -> str:
    """wake, working, busy, blocked, held, first_prompt or unknown."""
    if held is not None and (agent.status, agent.seq) == held:
        return "held"
    if agent.status in WAKE_STATUSES:
        if agent.claude_status == "busy":
            return "busy"
        if agent.claude_status == "waiting":
            return "blocked"
        if session.turns == 0:
            return "first_prompt"
        return WAKE
    if agent.status in ("working", "blocked"):
        return agent.status
    return "unknown"
