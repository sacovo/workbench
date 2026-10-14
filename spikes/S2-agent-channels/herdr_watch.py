#!/usr/bin/env python3
"""Record the probe agent's state from three sources, with wall-clock timestamps.

  push   herdr socket API events.subscribe, pane.agent_status_changed filtered to one pane
  poll   `herdr agent get <pane>` every --poll-ms, logged when status or state_change_seq changes
  claude `claude agents --json` every --claude-ms, the entry whose cwd is --cwd, logged on change

Read-only: subscribes and reads; never sends input. Writes JSON lines to --out until SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import time

stop = threading.Event()
lock = threading.Lock()


def emit(out, rec):
    rec = {"ts": time.time(), **rec}
    with lock:
        out.write(json.dumps(rec) + "\n")
        out.flush()


def push(out, pane: str):
    path = os.environ["HERDR_SOCKET_PATH"]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    req = {"id": "s2-watch", "method": "events.subscribe",
           "params": {"subscriptions": [{"type": "pane.agent_status_changed", "pane_id": pane}]}}
    s.sendall((json.dumps(req) + "\n").encode())
    s.settimeout(0.5)
    buf = b""
    while not stop.is_set():
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            emit(out, {"src": "push", "closed": True})
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                msg = {"raw": line.decode(errors="replace")}
            emit(out, {"src": "push", "msg": msg})
    s.close()


def poll(out, pane: str, every: float):
    last = None
    while not stop.is_set():
        t0 = time.time()
        p = subprocess.run(["herdr", "agent", "get", pane], capture_output=True, text=True)
        dt = time.time() - t0
        try:
            a = json.loads(p.stdout)["result"]["agent"]
            key = (a.get("agent_status"), a.get("state_change_seq"))
            rec = {"status": a.get("agent_status"), "seq": a.get("state_change_seq"),
                   "revision": a.get("revision"), "session": (a.get("agent_session") or {}).get("value")}
        except Exception:
            key = ("error", p.returncode, p.stderr[:200])
            rec = {"error": p.stderr[:300], "rc": p.returncode}
        if key != last:
            emit(out, {"src": "poll", "call_s": round(dt, 4), **rec})
            last = key
        stop.wait(max(0.0, every - (time.time() - t0)))


def claude_agents(out, cwd: str, every: float):
    last = None
    while not stop.is_set():
        t0 = time.time()
        p = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=20)
        try:
            mine = [a for a in json.loads(p.stdout) if a.get("cwd") == cwd]
            key = tuple((a.get("sessionId"), a.get("status")) for a in mine)
            rec = {"entries": mine}
        except Exception:
            key = ("error", p.returncode)
            rec = {"error": p.stderr[:300], "rc": p.returncode}
        if key != last:
            emit(out, {"src": "claude_agents", "call_s": round(time.time() - t0, 3), **rec})
            last = key
        stop.wait(max(0.0, every - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pane", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--poll-ms", type=int, default=200)
    ap.add_argument("--claude-ms", type=int, default=1000)
    a = ap.parse_args()
    out = open(a.out, "a", encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    emit(out, {"src": "watch", "started": True, "pane": a.pane})
    threads = [threading.Thread(target=push, args=(out, a.pane), daemon=True),
               threading.Thread(target=poll, args=(out, a.pane, a.poll_ms / 1000), daemon=True),
               threading.Thread(target=claude_agents, args=(out, a.cwd, a.claude_ms / 1000), daemon=True)]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(0.5)
    emit(out, {"src": "watch", "stopped": True})


if __name__ == "__main__":
    main()
