"""Budgets of the agent link, gated by tests/test_agentlink_hookscript.py.

They belong in the project's single budgets file (docs/plan.md, Verification) once one exists.

HOOK_DOWN_MAX_MS: every single run of a hook, through `/bin/sh -c` as Claude Code runs it, with
    no hub (no hub.json, a stale one, or nothing listening on its port). A hook runs on every
    prompt and every tool call; without a hub it must cost nothing noticeable. Spike S2 measured
    17 ms at worst, its reviewer 45 ms at worst on a loaded machine.
HOOK_UP_P95_MS: p95 of a hook with the hub up and answering from memory, and with a hub that
    accepts the connection but never answers (bounded by the script's own deadline,
    plugin/hooks/wb_hook.py DEADLINE_S). Spike S2: 22 ms up; 122 ms hung, 150 ms hung under load
    with a 100 ms request timeout, hence the shorter deadline now.
"""

HOOK_DOWN_MAX_MS = 50.0
HOOK_UP_P95_MS = 150.0
