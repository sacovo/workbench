"""Stop hook run with asyncRewake: long-poll the hub; exit 2 when a new owner item arrives.

Claude Code runs it in the background at every stop. Exit 2 makes Claude Code queue a
notification with the stderr text for the model; any other exit does nothing. The hub retires
this poller when a newer one registers or the session shows working activity.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s2common as c  # noqa: E402

WAIT_S = 3500  # hub-side long-poll; the hook's own timeout in settings.json is 3600 s


def main() -> int:
    raw = c.read_stdin()
    sock = c.connect()
    if sock is None:
        return 0
    inp = c.parse(raw)
    c.record(raw, "Stop-rewake")
    res = c.request(sock, f"/wait?session={c.safe(inp.get('session_id'))}&token={os.getpid()}&timeout={WAIT_S}",
                    total_timeout=WAIT_S + 30)
    if res and res.get("action") == "wake":
        ids = ", ".join(res["items"])
        sys.stderr.write(f"Owner answered {ids}. Run: curl -s {c.HUB_URL}/inbox\n")
        return 2
    return 0


try:
    code = main()
except Exception:
    code = 0
sys.exit(code)
