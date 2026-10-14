"""Claude Code hook of the workbench plugin (hooks.json runs it for four events).

    /usr/bin/python3 -I -S wb_hook.py session-start | user-prompt-submit | post-tool-use | stop

It finds the workbench project (from CLAUDE_PROJECT_DIR, else its working directory, walking up to
the first pyproject.toml with a [tool.workbench] table), reads the running hub's pid, port and
token from <root>/.workbench/hub.json, posts a short summary of the hook input to the hub and
prints what the hub answers under "output".

Contract with the hub (workbench.agentlink: AgentLink.handle_hook, routes.py):

    POST /api/hooks/<event> HTTP/1.0, headers Host 127.0.0.1:<port>, X-Workbench-Token
    {"session": "<session id>", "pane": "<HERDR_PANE_ID>" | null, "cwd": "<cwd>" | null, ...}
        session-start        + "source", "agent_type", "herdr_socket"
        user-prompt-submit   + "pointer_prompt": the prompt is a typed workbench pointer
        post-tool-use        + "tool", "pulled": the call ran `wbench inbox`
        stop                 + "stop_hook_active", as Claude Code sent it
    -> 200 {"output": <JSON object to print> | null}

The prompt text and the tool's input and output never leave this process.

Rules (spike S2 and its review):
- Standard library only, HTTP/1.0 over a raw socket: importing urllib alone costs about 40 ms.
  `json` (about 9 ms) is imported only once a hub.json exists.
- Fail open: no project, no hub.json, a dead or slow hub, an error status, a malformed answer or
  any exception: exit 0 and print nothing. One deadline covers connect, send and receive.
- Nothing for subagents (their hook input carries agent_id): a pointer in a subagent's context
  could make it run `wbench inbox` and take the owner's events away from the main agent.
- Two checks do not depend on the hub: a Stop block is printed only when stop_hook_active is the
  JSON boolean false, and only output of the expected shape is printed.
- SessionStart writes `unset VIRTUAL_ENV` and `export WB_SESSION=<session id>` to CLAUDE_ENV_FILE
  when Claude Code provides it, hub or no hub: the agent's Bash commands then run uv against the
  project's own environment, and wbench can record which session it serves.
"""

import os
import sys
import time

DEADLINE_S = 0.08  # connect + send + receive; keeps a hung hub under the 150 ms budget
MAX_ANSWER = 1 << 20  # bytes
POINTER_PREFIX = "Workbench:"
EVENTS = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "post-tool-use": "PostToolUse",
    "stop": "Stop",
}
ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
PANE_CHARS = ID_CHARS | {":"}


def plain(value, chars, limit):
    """A non-empty string of at most `limit` characters, all from `chars`."""
    return isinstance(value, str) and 0 < len(value) <= limit and all(c in chars for c in value)


def has_workbench_table(text):
    for line in text.splitlines():
        line = line.split(b"#", 1)[0].replace(b" ", b"").replace(b"\t", b"")
        if line == b"[tool.workbench]" or line.startswith(b"[tool.workbench."):
            return True
    return False


def find_root(start):
    here = os.path.abspath(start)
    while True:
        try:
            with open(os.path.join(here, "pyproject.toml"), "rb") as f:
                text = f.read(1 << 20)
        except OSError:
            text = b""
        if has_workbench_table(text):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def write_env_file(session):
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path:
        return
    lines = ["unset VIRTUAL_ENV", "export WB_SESSION=" + session]
    try:
        with open(path, "a+", encoding="utf-8") as f:
            f.seek(0)
            text = f.read()
            have = set(text.splitlines())
            new = [line for line in lines if line not in have]
            if new:
                f.write(("\n" if text and not text.endswith("\n") else "") + "".join(line + "\n" for line in new))
    except OSError:
        pass


def hub_address(root, json):
    try:
        with open(os.path.join(root, ".workbench", "hub.json"), "rb") as f:
            data = json.loads(f.read(4096))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, port, token = data.get("pid"), data.get("port"), data.get("token")
    if type(pid) is not int or type(port) is not int or not 0 < port < 65536 or pid <= 0:
        return None
    if not plain(token, ID_CHARS, 256):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # a stale hub.json: the hub is gone
    except OSError:
        pass  # alive, but not ours to signal
    return port, token


def post(port, token, path, body):
    """POST over HTTP/1.0 within DEADLINE_S; the response body on 200, else None."""
    import socket

    deadline = time.monotonic() + DEADLINE_S
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    chunks, size = [], 0
    try:
        sock.settimeout(DEADLINE_S)
        sock.connect(("127.0.0.1", port))
        head = (
            f"POST {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Workbench-Token: {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(head.encode("ascii") + body)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_ANSWER:
                return None
    except OSError:
        return None
    finally:
        sock.close()
    head, sep, payload = b"".join(chunks).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if not sep or len(status) < 2 or status[1] != b"200":
        return None
    return payload


def pulled(inp):
    """Did this tool call run `wbench inbox` (which marks the owner's events delivered)?"""
    tool = inp.get("tool_name")
    if isinstance(tool, str) and tool.startswith("mcp__") and "workbench" in tool and tool.endswith("__inbox"):
        return True
    if tool != "Bash":
        return False
    command = (inp.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "--no-ack" in command:
        return False
    for ch in ";&|()`\n":
        command = command.replace(ch, " ")
    words = command.split()
    for i, word in enumerate(words):
        if word == "wbench" or word.endswith("/wbench"):
            j = i + 1
            while j < len(words) and words[j].startswith("-"):  # wbench's own options: -C DIR, --json
                j += 2 if words[j] == "-C" else 1
            if j < len(words) and words[j] == "inbox":
                return True
    return False


def summary(event, inp, session):
    pane = os.environ.get("HERDR_PANE_ID")
    cwd = inp.get("cwd")
    out = {
        "session": session,
        "pane": pane if plain(pane, PANE_CHARS, 64) else None,
        "cwd": cwd if isinstance(cwd, str) and 0 < len(cwd) <= 4096 else None,
    }
    if event == "session-start":
        source, agent_type = inp.get("source"), inp.get("agent_type")
        socket_path = os.environ.get("HERDR_SOCKET_PATH")
        out["source"] = source if plain(source, ID_CHARS, 32) else None
        out["agent_type"] = agent_type if isinstance(agent_type, str) and 0 < len(agent_type) <= 256 else None
        out["herdr_socket"] = (
            socket_path if isinstance(socket_path, str) and socket_path.startswith("/") and len(socket_path) <= 4096 else None
        )
    elif event == "user-prompt-submit":
        prompt = inp.get("prompt")
        out["pointer_prompt"] = isinstance(prompt, str) and prompt.lstrip().startswith(POINTER_PREFIX)
    elif event == "post-tool-use":
        tool = inp.get("tool_name")
        out["tool"] = tool if isinstance(tool, str) and 0 < len(tool) <= 128 else None
        out["pulled"] = pulled(inp)
    else:
        active = inp.get("stop_hook_active")
        out["stop_hook_active"] = active if active is None or isinstance(active, (bool, int, float, str)) else None
    return out


def checked(event, output, inp, json):
    """The text to print for the hub's `output`, if it has the shape this event allows."""
    if not isinstance(output, dict) or not output:
        return None
    if event in ("user-prompt-submit", "post-tool-use"):
        spec = output.get("hookSpecificOutput")
        if set(output) != {"hookSpecificOutput"} or not isinstance(spec, dict):
            return None
        if set(spec) != {"hookEventName", "additionalContext"} or spec["hookEventName"] != EVENTS[event]:
            return None
        if not isinstance(spec["additionalContext"], str) or not spec["additionalContext"]:
            return None
    elif event == "stop":
        if inp.get("stop_hook_active") is not False:  # strict: only the JSON boolean false
            return None
        if set(output) != {"decision", "reason"} or output["decision"] != "block":
            return None
        if not isinstance(output["reason"], str) or not output["reason"]:
            return None
    else:
        return None
    return json.dumps(output)


def main(argv):
    if len(argv) != 2 or argv[1] not in EVENTS:
        return
    event = argv[1]
    raw = sys.stdin.buffer.read()  # always drain stdin, so Claude Code's write never fails
    root = find_root(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if root is None:
        return
    if event != "session-start" and not os.path.isfile(os.path.join(root, ".workbench", "hub.json")):
        return
    import json

    try:
        inp = json.loads(raw)
    except ValueError:
        return
    if not isinstance(inp, dict) or inp.get("agent_id"):
        return
    session = inp.get("session_id")
    if not plain(session, ID_CHARS, 128):
        return
    if event == "session-start":
        write_env_file(session)
    address = hub_address(root, json)
    if address is None:
        return
    port, token = address
    body = json.dumps(summary(event, inp, session)).encode("utf-8")
    answer = post(port, token, "/api/hooks/" + event, body)
    if answer is None:
        return
    try:
        reply = json.loads(answer)
    except ValueError:
        return
    text = checked(event, reply.get("output") if isinstance(reply, dict) else None, inp, json)
    if text:
        sys.stdout.write(text)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:
        pass
    sys.exit(0)
