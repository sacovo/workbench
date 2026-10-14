"""Stop hook: if owner items are unread, block the stop once and point the agent at the inbox.

Blocks only when stop_hook_active is false, so the stop that follows a block is always allowed,
whatever is unread by then. The reason is a fixed pointer; the content is pulled by the agent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s2common as c  # noqa: E402


def main() -> None:
    raw = c.read_stdin()
    sock = c.connect()
    if sock is None:
        return
    inp = c.parse(raw)
    active = bool(inp.get("stop_hook_active"))
    c.record(raw, f"Stop-active{int(active)}")
    res = c.request(sock, f"/hook/stop?session={c.safe(inp.get('session_id'))}&active={int(active)}")
    if active or not res or not res.get("unread"):
        return
    import json

    ids = ", ".join(res["unread"])
    reason = (f"Workbench: owner item {ids} is unread. Run exactly: curl -s {c.HUB_URL}/inbox "
              "and repeat each item text verbatim.")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))


try:
    main()
except Exception:
    pass
sys.exit(0)
