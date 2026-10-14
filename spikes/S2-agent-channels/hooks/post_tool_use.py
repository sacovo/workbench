"""PostToolUse hook: deliver unread workbench items as additionalContext after each tool call."""

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
    c.record(raw, "PostToolUse")
    path = (f"/hook/unread?event=PostToolUse&session={c.safe(inp.get('session_id'))}"
            f"&tool={c.safe(inp.get('tool_name'))}")
    res = c.request(sock, path)
    if not res or not res.get("items"):
        return
    import json

    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext": c.format_items(res["items"], "PostToolUse")}}
    sys.stdout.write(json.dumps(out))


try:
    main()
except Exception:
    pass
sys.exit(0)
