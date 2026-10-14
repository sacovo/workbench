"""Notification hook: tell the hub what Claude Code notified (e.g. permission_prompt). No output."""

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
    c.record(raw, "Notification")
    c.request(sock, f"/hook/notify?session={c.safe(inp.get('session_id'))}"
                    f"&type={c.safe(inp.get('notification_type'))}")


try:
    main()
except Exception:
    pass
sys.exit(0)
