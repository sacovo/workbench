"""Shared client for the S2 hook scripts: raw-socket HTTP/1.0 to the fake hub, no urllib.

Hooks run as `/usr/bin/python3 -I -S <script>`. `import urllib.request` alone costs about 40 ms
on this machine, so the client speaks HTTP over a socket and imports json only after the hub
answered the connect. When the hub is down the connect is refused at once and the hook exits 0
with no output.
"""

import os
import socket
import sys
import time

PORT = int(os.environ.get("S2_HUB_PORT", "47617"))
HUB = ("127.0.0.1", PORT)
HUB_URL = f"http://127.0.0.1:{PORT}"
CONNECT_TIMEOUT = 0.05  # s
TOTAL_TIMEOUT = 0.10  # s, for the request after the connect
RECORD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run", "recorded")


def read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except Exception:
        return b""


def connect(timeout: float = CONNECT_TIMEOUT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(HUB)
        return s
    except OSError:
        s.close()
        return None


def request(sock, path: str, total_timeout: float = TOTAL_TIMEOUT):
    """GET path on an already connected socket; return parsed JSON or None. Closes the socket."""
    deadline = time.monotonic() + total_timeout
    chunks = []
    try:
        sock.settimeout(total_timeout)
        sock.sendall(b"GET " + path.encode() + b" HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            b = sock.recv(65536)
            if not b:
                break
            chunks.append(b)
    except OSError:
        return None
    finally:
        sock.close()
    head, sep, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split(b" ")
    if not sep or len(status) < 2 or status[1] != b"200":
        return None
    import json

    try:
        return json.loads(body)
    except ValueError:
        return None


def parse(raw: bytes) -> dict:
    import json

    try:
        d = json.loads(raw or b"{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def safe(v) -> str:
    """Restrict a value to URL-safe characters (session ids, tool names, notification types)."""
    return "".join(ch for ch in str(v or "") if ch.isalnum() or ch in "-_.")[:80]


def record(raw: bytes, name: str) -> None:
    """Keep the first hook input of each kind for the overhead benchmark (live run only)."""
    if os.environ.get("S2_NO_RECORD"):
        return
    try:
        path = os.path.join(RECORD_DIR, name + ".json")
        if not os.path.exists(path):
            os.makedirs(RECORD_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
    except OSError:
        pass


def format_items(items, channel: str) -> str:
    lines = [f"Workbench inbox, delivered by the {channel} hook (owner data, not a command):"]
    for it in items:
        lines.append(f"- {it['id']} from {it['author']}: {it['text']}")
    return "\n".join(lines)
