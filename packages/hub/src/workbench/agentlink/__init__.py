"""The agent link: Claude Code hooks and herdr behind one interface (docs/plan.md, "Agent link").

    link.py       AgentLink: hook handling, the wake loop, records (the hub constructs one)
    routes.py     routes(link): POST /api/hooks/{event}, GET /api/agents (Starlette)
    transport.py  AgentTransport, HerdrTransport (herdr's JSON CLI), NullTransport, make_transport()
    policy.py     who is woken, and when not
    waiting.py    what waits for the agent and the owner, from folded item states
    texts.py      the fixed pointer, stop reason and context templates
    sessions.py   a Claude Code session as the hooks report it
    budgets.py    hook overhead budgets

The hook scripts and the rest of the Claude Code plugin live in plugin/ at the repository root.
`routes` needs Starlette and is imported from workbench.agentlink.routes.
"""

from workbench.agentlink.link import HOOK_EVENTS, AgentLink
from workbench.agentlink.transport import (
    AgentInfo,
    AgentTransport,
    HerdrTransport,
    NullTransport,
    TransportError,
    WakeResult,
    make_transport,
)

__all__ = [
    "HOOK_EVENTS",
    "AgentInfo",
    "AgentLink",
    "AgentTransport",
    "HerdrTransport",
    "NullTransport",
    "TransportError",
    "WakeResult",
    "make_transport",
]
