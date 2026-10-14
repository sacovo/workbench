"""UserPromptSubmit hook: deliver unread workbench items as additionalContext."""

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
    c.record(raw, "UserPromptSubmit")
    res = c.request(sock, f"/hook/unread?event=UserPromptSubmit&session={c.safe(inp.get('session_id'))}")
    if not res or not res.get("items"):
        return
    import json

    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "additionalContext": c.format_items(res["items"], "UserPromptSubmit")}}
    sys.stdout.write(json.dumps(out))


try:
    main()
except Exception:
    pass
sys.exit(0)
