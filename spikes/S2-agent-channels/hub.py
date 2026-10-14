#!/usr/bin/env python3
"""Fake workbench hub for spike S2 (stdlib only, in memory, 127.0.0.1).

Holds owner items ("unread" until delivered), answers the hook scripts, and serves a long-poll
for the asyncRewake poller. Every request that changes or reads delivery state is appended to an
event log (JSON lines) with a wall-clock timestamp, so the driver can line it up with herdr and
transcript timestamps.

Endpoints
  GET  /health
  GET  /hook/unread?event=E&session=S[&tool=T]   UserPromptSubmit / PostToolUse: return unread
                                                   items and mark them delivered via E
  GET  /hook/stop?session=S&active=0|1            Stop: return unread item ids, deliver nothing
  GET  /hook/notify?session=S&type=T              Notification: record only
  GET  /inbox                                     the agent's pull (curl): unread items, marked
                                                   delivered via "inbox"
  GET  /wait?session=S&token=T&timeout=SEC        rewake long-poll (see Hub.wait)
  GET  /state                                     full snapshot
  GET  /events?since=N&timeout=SEC                event log from index N, long-poll if empty
  POST /items   {"text", "author"?, "id"?}        add an item now
  POST /arm     {"on": E, "text", "author"?}      add an item right after the next call of E
                                                   (E = hook event name or "inbox")
  POST /config  {"rewake": bool}
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Hook calls that mean "the agent is working again"; they retire a waiting rewake poller.
ACTIVE_EVENTS = {"UserPromptSubmit", "PostToolUse"}


class Hub:
    def __init__(self, log_path: Path):
        self.cv = threading.Condition()
        self.items: list[dict] = []
        self.seq = 0
        self.armed: list[dict] = []
        self.pollers: dict[str, dict] = {}
        self.activity: dict[str, list[dict]] = {}
        self.config = {"rewake": True}
        self.events: list[dict] = []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log_path, "a", buffering=1, encoding="utf-8")

    # all methods below expect self.cv to be held
    def _event(self, kind: str, **kw) -> dict:
        ev = {"i": len(self.events), "ts": time.time(), "kind": kind, **kw}
        self.events.append(ev)
        self.log.write(json.dumps(ev) + "\n")
        self.cv.notify_all()
        return ev

    def _add(self, text: str, author: str = "owner", item_id: str | None = None, via: str = "api") -> dict:
        self.seq += 1
        item = {
            "id": item_id or f"R-{self.seq:04d}",
            "seq": self.seq,
            "author": author,
            "text": text,
            "created": time.time(),
            "delivered": None,
            "wake_sent": False,
        }
        self.items.append(item)
        self._event("item_added", item=item["id"], seq=item["seq"], via=via, text=text)
        return item

    def _unread(self) -> list[dict]:
        return [it for it in self.items if it["delivered"] is None]

    def _deliver(self, items: list[dict], channel: str, session: str | None) -> None:
        now = time.time()
        for it in items:
            it["delivered"] = {"channel": channel, "ts": now, "session": session}

    def _fire_armed(self, on: str) -> None:
        keep = []
        for a in self.armed:
            if a["on"] == on:
                self._add(a["text"], a.get("author", "owner"), a.get("id"), via=f"armed:{on}")
            else:
                keep.append(a)
        self.armed = keep

    def _touch(self, session: str, event: str) -> None:
        self.activity.setdefault(session, []).append({"ts": time.time(), "event": event})

    @staticmethod
    def _public(it: dict) -> dict:
        return {k: it[k] for k in ("id", "author", "text", "created")}

    # public operations
    def add(self, text, author="owner", item_id=None):
        with self.cv:
            return self._add(text, author, item_id)

    def arm(self, on, text, author="owner", item_id=None):
        with self.cv:
            entry = {"on": on, "text": text, "author": author, "id": item_id}
            self.armed.append(entry)
            self._event("armed", on=on, text=text)
            return entry

    def set_config(self, **kw):
        with self.cv:
            self.config.update(kw)
            self._event("config", **kw)
            return dict(self.config)

    def hook_unread(self, event: str, session: str, tool: str | None):
        with self.cv:
            self._touch(session, event)
            items = self._unread()
            self._deliver(items, event, session)
            self._event("hook_call", hook=event, session=session, tool=tool,
                        delivered=[it["id"] for it in items])
            out = {"items": [self._public(it) for it in items]}
            self._fire_armed(event)
            return out

    def hook_stop(self, session: str, active: bool):
        with self.cv:
            self._touch(session, "Stop")
            ids = [it["id"] for it in self._unread()]
            self._event("hook_call", hook="Stop", session=session, stop_hook_active=active, unread=ids)
            self._fire_armed("Stop")
            return {"unread": ids}

    def hook_notify(self, session: str, ntype: str):
        with self.cv:
            self._touch(session, "Notification")
            self._event("hook_call", hook="Notification", session=session, notification_type=ntype)
            return {}

    def inbox(self):
        with self.cv:
            items = self._unread()
            self._deliver(items, "inbox", None)
            self._event("inbox_pull", delivered=[it["id"] for it in items])
            out = {"items": [self._public(it) for it in items]}
            self._fire_armed("inbox")
            return out

    def wait(self, session: str, token: str, timeout: float):
        """Rewake long-poll. Returns "wake" for items added after registration that nobody has
        delivered or woken for, once; retires itself when a newer poller registers for the same
        session, when the session shows working activity after registration (its blocked stop
        continued, or a new prompt arrived), when rewake is disabled, or on timeout."""
        with self.cv:
            reg = {"token": token, "reg_ts": time.time(), "reg_seq": self.seq}
            self.pollers[session] = reg
            self._event("poller_registered", session=session, token=token, reg_seq=self.seq)
            deadline = time.time() + timeout
            while True:
                if not self.config["rewake"]:
                    result = {"action": "disabled"}
                    break
                if self.pollers.get(session) is not reg:
                    result = {"action": "superseded"}
                    break
                acts = [a for a in self.activity.get(session, [])
                        if a["ts"] > reg["reg_ts"] and a["event"] in ACTIVE_EVENTS]
                if acts:
                    result = {"action": "superseded_by_activity", "event": acts[0]["event"]}
                    break
                new = [it for it in self.items if it["seq"] > reg["reg_seq"]
                       and it["delivered"] is None and not it["wake_sent"]]
                if new:
                    for it in new:
                        it["wake_sent"] = True
                    result = {"action": "wake", "items": [it["id"] for it in new]}
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    result = {"action": "timeout"}
                    break
                self.cv.wait(min(remaining, 1.0))
            if self.pollers.get(session) is reg:
                del self.pollers[session]
            self._event("poller_returned", session=session, token=token, **result)
            return result

    def state(self):
        with self.cv:
            return {
                "items": self.items,
                "armed": self.armed,
                "pollers": self.pollers,
                "config": self.config,
                "n_events": len(self.events),
            }

    def events_since(self, since: int, timeout: float):
        with self.cv:
            deadline = time.time() + timeout
            while len(self.events) <= since:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cv.wait(remaining)
            return {"events": self.events[since:], "next": len(self.events)}


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):  # quiet; the event log is the record
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # client gone (e.g. a killed poller)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            s = q.get("session", "")
            if u.path == "/health":
                return self._send({"ok": True})
            if u.path == "/hook/unread":
                return self._send(hub.hook_unread(q.get("event", "?"), s, q.get("tool")))
            if u.path == "/hook/stop":
                return self._send(hub.hook_stop(s, q.get("active") == "1"))
            if u.path == "/hook/notify":
                return self._send(hub.hook_notify(s, q.get("type", "")))
            if u.path == "/inbox":
                return self._send(hub.inbox())
            if u.path == "/wait":
                return self._send(hub.wait(s, q.get("token", ""), float(q.get("timeout", "60"))))
            if u.path == "/state":
                return self._send(hub.state())
            if u.path == "/events":
                return self._send(hub.events_since(int(q.get("since", "0")), float(q.get("timeout", "0"))))
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send({"error": "bad json"}, 400)
            if u.path == "/items":
                return self._send(hub.add(body["text"], body.get("author", "owner"), body.get("id")))
            if u.path == "/arm":
                return self._send(hub.arm(body["on"], body["text"], body.get("author", "owner"), body.get("id")))
            if u.path == "/config":
                return self._send(hub.set_config(**body))
            return self._send({"error": "not found"}, 404)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=47617)
    ap.add_argument("--log", default=str(Path(__file__).resolve().parent / "run" / "hub-events.jsonl"))
    args = ap.parse_args()
    hub = Hub(Path(args.log))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(hub))
    srv.daemon_threads = True

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with hub.cv:
        hub._event("hub_started", port=args.port)
    print(f"hub listening on 127.0.0.1:{args.port}, log {args.log}", flush=True)
    srv.serve_forever(poll_interval=0.2)
    with hub.cv:
        hub._event("hub_stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()
