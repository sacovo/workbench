#!/usr/bin/env python3
"""Offline checks and overhead benchmark for the S2 hook scripts (no agent, no API calls).

  bench_hooks.py check        functional checks against a private hub on port 47630
  bench_hooks.py bench [-n N] time every hook as a subprocess with recorded hook JSON on stdin,
                              hub up (private hub on 47630), hub down (closed port 47631) and
                              hub hung (a listening socket that never answers, 47632)

Hook input comes from run/recorded/*.json (written by the hooks during the live run); a
synthetic input is used only when no recording exists, and the output says which.
Results go to run/bench.json (bench) or run/check.json (check).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
RUN = SPIKE / "run"
HOOKS = SPIKE / "hooks"
PY = ["/usr/bin/python3", "-I", "-S"]
UP, DOWN, HUNG = 47630, 47631, 47632

SYNTH = {
    "UserPromptSubmit": {"session_id": "bench", "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
    "PostToolUse": {"session_id": "bench", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "sleep 8"}, "tool_response": {"stdout": ""}},
    "Stop-active0": {"session_id": "bench", "hook_event_name": "Stop", "stop_hook_active": False},
    "Stop-active1": {"session_id": "bench", "hook_event_name": "Stop", "stop_hook_active": True},
    "Notification": {"session_id": "bench", "hook_event_name": "Notification",
                     "notification_type": "permission_prompt", "message": "x"},
    "Stop-rewake": {"session_id": "bench-rewake", "hook_event_name": "Stop", "stop_hook_active": False},
}
SCRIPT = {
    "UserPromptSubmit": "user_prompt_submit.py",
    "PostToolUse": "post_tool_use.py",
    "Stop-active0": "stop.py",
    "Stop-active1": "stop.py",
    "Notification": "notify.py",
    "Stop-rewake": "rewake_poller.py",
}


def hook_input(name: str) -> tuple[bytes, str]:
    p = RUN / "recorded" / f"{name}.json"
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes(), "recorded"
    return json.dumps(SYNTH[name]).encode(), "synthetic"


def run_hook(name: str, stdin: bytes, port: int, via_shell: bool = False, timeout: float = 10):
    cmd = PY + [str(HOOKS / SCRIPT[name])]
    env = dict(os.environ, S2_HUB_PORT=str(port), S2_NO_RECORD="1")
    if via_shell:  # how Claude Code runs it: through the user's shell
        cmd = [os.environ.get("SHELL", "/bin/sh"), "-c", " ".join(cmd)]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, input=stdin, capture_output=True, env=env, timeout=timeout)
    return (time.perf_counter() - t0) * 1000, p.returncode, p.stdout, p.stderr


def hub_call(port: int, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def start_hub(port: int, log: Path) -> subprocess.Popen:
    log.unlink(missing_ok=True)
    p = subprocess.Popen(["/usr/bin/python3", str(SPIKE / "hub.py"), "--port", str(port), "--log", str(log)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            hub_call(port, "GET", "/health")
            return p
        except OSError:
            time.sleep(0.02)
    p.kill()
    raise SystemExit("private hub did not start")


def port_closed(port: int) -> bool:
    s = socket.socket()
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def check() -> int:
    assert port_closed(DOWN), f"port {DOWN} must be closed for the hub-down check"
    hub = start_hub(UP, RUN / "check-hub-events.jsonl")
    results, failures = [], []

    def expect(label, cond, detail=""):
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        results.append({"check": label, "ok": bool(cond), "detail": detail if isinstance(detail, str) else repr(detail)})
        if not cond:
            failures.append(label)

    try:
        ups = json.dumps(SYNTH["UserPromptSubmit"]).encode()
        _, rc, out, _ = run_hook("UserPromptSubmit", ups, UP)
        expect("UPS, nothing unread: exit 0, no output", rc == 0 and out == b"", out[:200])
        hub_call(UP, "POST", "/items", {"text": "token WB-check1"})
        _, rc, out, _ = run_hook("UserPromptSubmit", ups, UP)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
        expect("UPS, one unread: additionalContext carries the text", rc == 0 and "WB-check1" in ctx, ctx)
        _, rc, out, _ = run_hook("UserPromptSubmit", ups, UP)
        expect("UPS, item already delivered: no output", rc == 0 and out == b"", out[:200])

        hub_call(UP, "POST", "/items", {"text": "token WB-check2"})
        _, rc, out, _ = run_hook("PostToolUse", json.dumps(SYNTH["PostToolUse"]).encode(), UP)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
        expect("PostToolUse, one unread: additionalContext carries the text", rc == 0 and "WB-check2" in ctx, ctx)

        hub_call(UP, "POST", "/items", {"text": "token WB-check3"})
        _, rc, out, _ = run_hook("Stop-active0", json.dumps(SYNTH["Stop-active0"]).encode(), UP)
        dec = json.loads(out) if out else {}
        expect("Stop, unread and stop_hook_active false: block", rc == 0 and dec.get("decision") == "block", dec)
        _, rc, out, _ = run_hook("Stop-active1", json.dumps(SYNTH["Stop-active1"]).encode(), UP)
        expect("Stop, unread and stop_hook_active true: allow (no output)", rc == 0 and out == b"", out[:200])
        pulled = hub_call(UP, "GET", "/inbox")
        expect("inbox pull returns the unread item", [i["text"] for i in pulled["items"]] == ["token WB-check3"], pulled)
        _, rc, out, _ = run_hook("Stop-active0", json.dumps(SYNTH["Stop-active0"]).encode(), UP)
        expect("Stop, nothing unread: allow (no output)", rc == 0 and out == b"", out[:200])

        _, rc, out, _ = run_hook("Notification", json.dumps(SYNTH["Notification"]).encode(), UP)
        ev = hub_call(UP, "GET", "/events?since=0")["events"]
        expect("Notification recorded at the hub, no output",
               rc == 0 and out == b"" and any(e.get("notification_type") == "permission_prompt" for e in ev))

        # rewake poller: wake on a new item
        env = dict(os.environ, S2_HUB_PORT=str(UP), S2_NO_RECORD="1")
        p = subprocess.Popen(PY + [str(HOOKS / "rewake_poller.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p.stdin.write(json.dumps({"session_id": "s-wake"}).encode()); p.stdin.close()
        time.sleep(0.4)
        hub_call(UP, "POST", "/items", {"text": "answer WB-check4"})
        rc = p.wait(timeout=5); err = p.stderr.read().decode()
        expect("poller: new item -> exit 2 with a pointer on stderr", rc == 2 and "/inbox" in err, f"rc={rc} {err!r}")

        # rewake poller: retired by working activity (a PostToolUse call for the same session)
        p = subprocess.Popen(PY + [str(HOOKS / "rewake_poller.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p.stdin.write(json.dumps({"session_id": "s-act"}).encode()); p.stdin.close()
        time.sleep(0.4)
        run_hook("PostToolUse", json.dumps({**SYNTH["PostToolUse"], "session_id": "s-act"}).encode(), UP)
        hub_call(UP, "POST", "/items", {"text": "answer WB-check5"})
        rc = p.wait(timeout=5)
        expect("poller: working activity after registration -> exit 0", rc == 0, f"rc={rc}")
        hub_call(UP, "GET", "/inbox")

        # rewake poller: superseded by a newer poller for the same session
        p1 = subprocess.Popen(PY + [str(HOOKS / "rewake_poller.py")], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p1.stdin.write(json.dumps({"session_id": "s-sup"}).encode()); p1.stdin.close()
        time.sleep(0.4)
        p2 = subprocess.Popen(PY + [str(HOOKS / "rewake_poller.py")], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p2.stdin.write(json.dumps({"session_id": "s-sup"}).encode()); p2.stdin.close()
        rc1 = p1.wait(timeout=5)
        expect("poller: newer poller for the session -> older exits 0", rc1 == 0, f"rc={rc1}")
        hub_call(UP, "POST", "/config", {"rewake": False})
        rc2 = p2.wait(timeout=5)
        expect("poller: rewake disabled -> exit 0", rc2 == 0, f"rc={rc2}")

        # hub down: every hook exits 0, prints nothing, and is fast
        for name in SCRIPT:
            ms, rc, out, err = run_hook(name, json.dumps(SYNTH[name]).encode(), DOWN)
            expect(f"hub down: {name} exit 0, no output, under 50 ms", rc == 0 and out == b"" and err == b"" and ms < 50,
                   f"{ms:.1f} ms rc={rc}")
    finally:
        hub.send_signal(signal.SIGTERM)
        hub.wait(timeout=5)
    out = {"n_checks": len(results), "n_failed": len(failures), "results": results}
    (RUN / "check.json").write_text(json.dumps(out, indent=1))
    for r in results:
        print(("ok  " if r["ok"] else "FAIL") + "  " + r["check"] + ("" if r["ok"] else f"   {r['detail']}"))
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    return 1 if failures or not results else 0


def stats(xs):
    xs = sorted(xs)
    k = max(0, int(round(0.95 * len(xs))) - 1)
    return {"n": len(xs), "p50_ms": round(statistics.median(xs), 2), "p95_ms": round(xs[k], 2),
            "max_ms": round(xs[-1], 2), "min_ms": round(xs[0], 2)}


def bench(n: int) -> int:
    assert port_closed(DOWN), f"port {DOWN} must be closed"
    hung = socket.socket()
    hung.bind(("127.0.0.1", HUNG)); hung.listen(512)  # never accept(): connects succeed, no reply
    hub = start_hub(UP, RUN / "bench-hub-events.jsonl")
    rows = []
    try:
        for name in SCRIPT:
            stdin, source = hook_input(name)
            scen = ["up_empty", "down", "hung"] if name != "Stop-rewake" else ["down"]
            if name in ("UserPromptSubmit", "PostToolUse", "Stop-active0", "Stop-active1"):
                scen.insert(1, "up_item")
            if name == "UserPromptSubmit":
                scen.append("up_empty_via_shell")
            for sc in scen:
                times, bad = [], []
                hub_call(UP, "GET", "/inbox")  # clear unread
                if sc == "up_item" and name.startswith("Stop"):
                    hub_call(UP, "POST", "/items", {"text": "bench item"})  # stays unread for Stop
                for i in range(n):
                    if sc == "up_item" and not name.startswith("Stop"):
                        hub_call(UP, "POST", "/items", {"text": f"bench item {i}"})  # consumed per call
                    port = {"down": DOWN, "hung": HUNG}.get(sc, UP)
                    ms, rc, out, err = run_hook(name, stdin, port, via_shell=sc.endswith("via_shell"))
                    times.append(ms)
                    want_out = sc == "up_item" and name != "Stop-active1"
                    if rc != 0 or bool(out) != want_out or err:
                        bad.append({"i": i, "rc": rc, "out": out[:120].decode(errors="replace"),
                                    "err": err[:120].decode(errors="replace")})
                row = {"hook": name, "script": SCRIPT[name], "input": source, "scenario": sc, **stats(times),
                       "unexpected": bad[:3], "n_unexpected": len(bad)}
                rows.append(row)
                print(f"{name:16s} {sc:20s} {source:9s} p50 {row['p50_ms']:6.1f}  p95 {row['p95_ms']:6.1f}  "
                      f"max {row['max_ms']:6.1f} ms  unexpected {len(bad)}")
    finally:
        hub.send_signal(signal.SIGTERM)
        hub.wait(timeout=5)
        hung.close()
    meta = {"n_per_cell": n, "python": subprocess.run(PY[:1] + ["--version"], capture_output=True, text=True).stdout.strip(),
            "command": " ".join(PY) + " <script>", "shell": os.environ.get("SHELL"),
            "connect_timeout_s": 0.05, "request_timeout_s": 0.10, "ts": time.time()}
    (RUN / "bench.json").write_text(json.dumps({"meta": meta, "rows": rows}, indent=1))
    return 0 if rows and all(r["n_unexpected"] == 0 for r in rows) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["check", "bench"])
    ap.add_argument("-n", type=int, default=40)
    a = ap.parse_args()
    sys.exit(check() if a.what == "check" else bench(a.n))
