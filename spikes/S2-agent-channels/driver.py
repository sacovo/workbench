#!/usr/bin/env python3
"""Driver for the live part of spike S2. One subcommand per step; every command it runs is
appended to run/driver-log.jsonl with timestamps, return code and output.

  split            new pane right of the caller's pane (--no-focus), cwd examples/minimal
  start            herdr agent start s2probe in that pane (haiku, spike settings)
  get | read | list
  keys KEY...      send-keys to s2probe only (pane checked first)
  q1 .. q5         the questions; each writes run/<step>.json
  close            close the pane created by `split` (pane checked first)

Every command that sends input checks first that the name s2probe resolves to the pane this
driver created (run/agent.json). Nothing is ever sent to another pane.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
RUN = SPIKE / "run"
EXAMPLE = Path("/home/sandro/work/privat/workbench/examples/minimal")
HUB = "http://127.0.0.1:47617"
NAME = "s2probe"
AGENT_JSON = RUN / "agent.json"
CURL = f"curl -s {HUB}/inbox"


# ---------------------------------------------------------------- logging and processes
def log(rec: dict) -> None:
    with open(RUN / "driver-log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def sh(argv: list[str], timeout: float | None = 180, note: str = "") -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = "timeout", (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    t1 = time.time()
    rec = {"ts_start": t0, "ts_end": t1, "dur_s": round(t1 - t0, 3), "argv": argv, "rc": rc,
           "stdout": out[:6000], "stderr": err[:3000], "note": note}
    log(rec)
    return rec


def jparse(s: str):
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- hub
def hub(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, method=method)
    with urllib.request.urlopen(req, timeout=40) as r:
        out = json.loads(r.read())
    if method == "POST":
        log({"ts_start": time.time(), "hub": f"{method} {path}", "body": body, "result": out})
    return out


def n_events() -> int:
    return hub("GET", "/state")["n_events"]


def events_since(i: int) -> list[dict]:
    return hub("GET", f"/events?since={i}")["events"]


def wait_event(pred, since: int, timeout: float):
    deadline = time.time() + timeout
    i = since
    while time.time() < deadline:
        r = hub("GET", f"/events?since={i}&timeout={min(5.0, max(0.1, deadline - time.time()))}")
        for ev in r["events"]:
            if pred(ev):
                return ev
        i = r["next"]
    return None


def token() -> str:
    return "WB-" + secrets.token_hex(3).upper()


# ---------------------------------------------------------------- herdr, guarded
def my_pane() -> str:
    return json.loads(AGENT_JSON.read_text())["pane_id"]


def agent_get(note="") -> dict | None:
    r = sh(["herdr", "agent", "get", NAME], timeout=20, note=note)
    j = jparse(r["stdout"])
    return j["result"]["agent"] if j else None


def guard() -> dict:
    a = agent_get("guard")
    if not a or a.get("pane_id") != my_pane():
        raise SystemExit(f"refusing: {NAME} does not resolve to my pane {my_pane()}: {a}")
    return a


def status() -> str | None:
    a = agent_get("status")
    return a.get("agent_status") if a else None


def wait_status(want: set[str], timeout: float, every: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = status()
        if s in want:
            return s, time.time()
        time.sleep(every)
    return None, time.time()


def prompt(text: str, wait: bool = True, timeout_ms: int = 120000) -> dict:
    guard()
    argv = ["herdr", "agent", "prompt", NAME, text]
    if wait:
        argv += ["--wait", "--timeout", str(timeout_ms)]
    return sh(argv, timeout=timeout_ms / 1000 + 30, note="prompt")


def read_screen(source="visible", lines=60) -> str:
    r = sh(["herdr", "agent", "read", NAME, "--source", source, "--lines", str(lines)], timeout=20, note="read")
    j = jparse(r["stdout"])
    if j and "result" in j:
        res = j["result"]
        return res.get("text") or res.get("read", {}).get("text") or json.dumps(res)
    return r["stdout"]


# ---------------------------------------------------------------- transcript
def transcript_path() -> Path | None:
    for name in ("UserPromptSubmit", "Stop-active0", "PostToolUse"):
        p = RUN / "recorded" / f"{name}.json"
        if p.exists():
            tp = (jparse(p.read_text()) or {}).get("transcript_path")
            if tp:
                return Path(tp)
    a = agent_get("transcript lookup") or {}
    sess = (a.get("agent_session") or {}).get("value")
    if sess:
        hits = list(Path.home().joinpath(".claude", "projects").glob(f"*/{sess}.jsonl"))
        if hits:
            return hits[0]
    return None


def t_size() -> int:
    p = transcript_path()
    return p.stat().st_size if p and p.exists() else 0


def t_lines(offset: int) -> list[dict]:
    p = transcript_path()
    if not p or not p.exists():
        return []
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read()
    out = []
    for ln in data.splitlines():
        j = jparse(ln.decode(errors="replace"))
        if j is not None:
            out.append(j)
    return out


def summarize_line(j: dict) -> dict:
    """Compact view of a transcript line: type, role, content kinds, text snippet."""
    msg = j.get("message") or {}
    content = msg.get("content")
    kinds, text = [], ""
    if isinstance(content, str):
        kinds, text = ["str"], content
    elif isinstance(content, list):
        for c in content:
            k = c.get("type")
            kinds.append(k)
            if k == "text":
                text += c.get("text", "")
            elif k == "tool_use":
                text += json.dumps(c.get("input"))
            elif k == "tool_result":
                cc = c.get("content")
                text += cc if isinstance(cc, str) else json.dumps(cc)
    att = j.get("attachment")
    if att is not None:
        kinds.append("attachment:" + str(att.get("type")))
        text += json.dumps(att)[:1500]
    if j.get("type") == "system":
        text += str(j.get("content", ""))[:800]
        kinds.append("system:" + str(j.get("subtype")))
    return {"type": j.get("type"), "role": msg.get("role"), "kinds": kinds, "ts": j.get("timestamp"),
            "uuid": j.get("uuid"), "isMeta": j.get("isMeta"), "text": text[:700]}


def save_transcript_slice(step: str, offset: int) -> list[dict]:
    lines = t_lines(offset)
    summ = [summarize_line(j) for j in lines]
    (RUN / f"{step}-transcript.json").write_text(json.dumps(summ, indent=1))
    return summ


def with_token(summ: list[dict], tok: str) -> list[dict]:
    return [s for s in summ if tok in s["text"]]


def save(step: str, data: dict) -> None:
    (RUN / f"{step}.json").write_text(json.dumps(data, indent=1, default=str))
    print(json.dumps(data, indent=1, default=str)[:6000])


# ---------------------------------------------------------------- steps
def cmd_split():
    if AGENT_JSON.exists():
        raise SystemExit(f"{AGENT_JSON} exists; refusing to create a second pane")
    r = sh(["herdr", "pane", "split", "--current", "--direction", "right", "--cwd", str(EXAMPLE), "--no-focus"],
           note="split")
    j = jparse(r["stdout"])
    pane = j["result"]["pane"]["pane_id"]
    AGENT_JSON.write_text(json.dumps({"pane_id": pane, "created": time.time(), "split": j}, indent=1))
    print(json.dumps(j, indent=1))


def cmd_start():
    pane = my_pane()
    argv = ["herdr", "agent", "start", NAME, "--kind", "claude", "--pane", pane, "--timeout", "60000", "--",
            "--model", "haiku", "--settings", str(SPIKE / "settings.json"), "--strict-mcp-config",
            "--debug-file", str(RUN / "claude-debug.log")]
    r = sh(argv, timeout=90, note="start")
    print(r["rc"], r["stdout"], r["stderr"])


def cmd_keys(keys: list[str]):
    guard()
    r = sh(["herdr", "agent", "send-keys", NAME, *keys], timeout=20, note="send-keys")
    print(r["rc"], r["stdout"], r["stderr"])


def cmd_q1():
    tok = token()
    ev0, off = n_events(), t_size()
    item = hub("POST", "/items", {"text": f"Owner note: the release token is {tok}."})
    r = prompt("Repeat verbatim any workbench note you were given with this message.")
    time.sleep(1.0)
    evs = events_since(ev0)
    summ = save_transcript_slice("q1", off)
    hits = with_token(summ, tok)
    assistant_hits = [h for h in hits if h["type"] == "assistant"]
    save("q1", {"token": tok, "item": item["id"], "prompt": r, "hub_events": evs, "token_lines": hits,
                "pass": bool(assistant_hits) and any(e.get("hook") == "UserPromptSubmit" and item["id"] in e.get("delivered", []) for e in evs)})


def cmd_q2():
    tok = token()
    ev0, off = n_events(), t_size()
    box = {}
    th = threading.Thread(target=lambda: box.update(r=prompt(
        "Run the Bash command `sleep 8`. When it has finished, reply with any workbench note you received, verbatim.")))
    th.start()
    ups = wait_event(lambda e: e.get("hook") == "UserPromptSubmit", ev0, 30)
    time.sleep(4.0)
    mid = {"herdr_status": status(),
           "claude_agents": sh(["claude", "agents", "--json"], timeout=20, note="claude agents mid-turn")["stdout"][:3000]}
    item = hub("POST", "/items", {"text": f"Owner note added mid-turn: token {tok}."})
    t_add = time.time()
    th.join()
    time.sleep(1.0)
    evs = events_since(ev0)
    summ = save_transcript_slice("q2", off)
    hits = with_token(summ, tok)
    post = [e for e in evs if e.get("hook") == "PostToolUse" and item["id"] in e.get("delivered", [])]
    n_ups = sum(1 for e in evs if e.get("hook") == "UserPromptSubmit")
    save("q2", {"token": tok, "item": item["id"], "t_ups": ups and ups["ts"], "t_add": t_add,
                "mid_turn": mid, "prompt": box.get("r"), "hub_events": evs, "token_lines": hits,
                "n_user_prompt_submit": n_ups,
                "pass": bool(post) and n_ups == 1 and any(h["type"] == "assistant" for h in hits)})


def cmd_q3():
    tok_a, tok_b = token(), token()
    ev0, off = n_events(), t_size()
    hub("POST", "/arm", {"on": "UserPromptSubmit", "text": f"Owner answer: use token {tok_a}."})
    hub("POST", "/arm", {"on": "PostToolUse", "text": f"Late owner note {tok_b}."})
    r = prompt("Reply with just the word OK.")
    time.sleep(2.0)
    evs = events_since(ev0)
    summ = save_transcript_slice("q3", off)
    stops = [e for e in evs if e.get("hook") == "Stop"]
    pulls = [e for e in evs if e.get("kind") == "inbox_pull"]
    st = hub("GET", "/state")
    b_unread = [it["id"] for it in st["items"] if tok_b in it["text"] and it["delivered"] is None]
    save("q3", {"tok_a": tok_a, "tok_b": tok_b, "prompt": r, "hub_events": evs,
                "stops": stops, "inbox_pulls": pulls, "tok_b_unread_after": b_unread,
                "status_after": status(), "token_a_lines": with_token(summ, tok_a),
                "token_b_lines": with_token(summ, tok_b),
                "pass": (len(stops) == 2 and stops[0]["stop_hook_active"] is False and stops[1]["stop_hook_active"] is True
                         and bool(pulls) and bool(b_unread)
                         and any(h["type"] == "assistant" for h in with_token(summ, tok_a)))})


def cmd_q4a():
    s, _ = wait_status({"idle", "done"}, 60)
    time.sleep(5.0)
    st = hub("GET", "/state")
    before = agent_get("q4a before")
    tok = token()
    ev0, off = n_events(), t_size()
    item = hub("POST", "/items", {"text": f"Owner answered: approved, token {tok}."})
    t_add = time.time()
    wake = wait_event(lambda e: e.get("kind") == "poller_returned", ev0, 10)
    s1, t_working = wait_status({"working", "blocked"}, 60, every=0.05)
    s2, t_idle = wait_status({"idle", "done"}, 120) if s1 else (None, None)
    time.sleep(1.5)
    after = agent_get("q4a after")
    evs = events_since(ev0)
    summ = save_transcript_slice("q4a", off)
    hits = with_token(summ, tok)
    # A rewake turn does pass through UserPromptSubmit (measured on 2.1.283), so "no UPS call"
    # is not evidence that nothing was typed. Evidence used instead: the driver sent no prompt
    # in the window, and the turn's user entry is a <task-notification>.
    n_ups = sum(1 for e in evs if e.get("hook") == "UserPromptSubmit")
    pulls = [e for e in evs if e.get("kind") == "inbox_pull"]
    typed = driver_prompts_between(t_add - 1, (t_idle or time.time()))
    notif = [x for x in summ if x["type"] == "user" and "<task-notification>" in x["text"]]
    save("q4a", {"token": tok, "item": item["id"], "status_at_start": s, "pollers_before": st["pollers"],
                 "herdr_before": before, "herdr_after": after, "t_add": t_add, "poller_result": wake,
                 "t_working": t_working if s1 else None, "working_status": s1, "t_idle": t_idle, "idle_status": s2,
                 "hub_events": evs, "token_lines": hits, "n_user_prompt_submit": n_ups, "inbox_pulls": pulls,
                 "driver_prompts_in_window": typed, "task_notification_lines": notif,
                 "pass": bool(wake and wake.get("action") == "wake" and s1 and not typed and notif and pulls)})


def driver_prompts_between(t0: float, t1: float) -> list[dict]:
    out = []
    p = RUN / "driver-log.jsonl"
    for ln in p.read_text().splitlines():
        r = jparse(ln) or {}
        argv = r.get("argv") or []
        if argv[:3] == ["herdr", "agent", "prompt"] and r.get("ts_end", 0) >= t0 and r.get("ts_start", 0) <= t1:
            out.append({"ts_start": r["ts_start"], "argv": argv})
    return out


def cmd_q4b():
    hub("POST", "/config", {"rewake": False})
    s, _ = wait_status({"idle", "done"}, 60)
    time.sleep(2.0)
    before = agent_get("q4b before")
    tok = token()
    ev0, off = n_events(), t_size()
    item = hub("POST", "/items", {"text": f"Owner answered: go ahead, token {tok}."})
    text = f"Owner answered {item['id']}. Run: {CURL}"
    r = prompt(text)
    after_immediate = agent_get("q4b after (immediate)")
    time.sleep(2.0)
    after = agent_get("q4b after")
    evs = events_since(ev0)
    summ = save_transcript_slice("q4b", off)
    save("q4b", {"token": tok, "item": item["id"], "typed": text, "herdr_before": before, "prompt": r,
                 "herdr_after_immediate": after_immediate, "herdr_after": after, "hub_events": evs,
                 "token_lines": with_token(summ, tok),
                 "pass": r["rc"] == 0 and any(e.get("hook") == "UserPromptSubmit" for e in evs)})


def cmd_q5():
    s, _ = wait_status({"idle", "done"}, 60)
    ev0, off = n_events(), t_size()
    r1 = prompt("Run this exact Bash command: python3 -c 'print(6*7)'", timeout_ms=60000)
    g1 = agent_get("q5 blocked?")
    scr1 = read_screen()
    tok = token()
    item = hub("POST", "/items", {"text": f"Owner answered while blocked: token {tok}."})
    text = f"Owner answered {item['id']}. Run: {CURL}"
    n_before = n_events()
    guard()
    r2 = sh(["herdr", "agent", "prompt", NAME, text], timeout=30, note="prompt to blocked agent")
    time.sleep(2.0)
    scr2 = read_screen()
    g2 = agent_get("q5 after refused prompt")
    evs_mid = events_since(n_before)
    cmd_keys(["esc"])
    t_esc = time.time()
    s3, t3 = wait_status({"idle", "done"}, 30, every=0.05)
    time.sleep(2.0)
    g3 = agent_get("q5 after esc")
    scr3 = read_screen()
    evs = events_since(ev0)
    summ = save_transcript_slice("q5", off)
    st = hub("GET", "/state")
    typed_in_transcript = [x for x in summ if f"Owner answered {item['id']}" in x["text"]]
    item_state = [it for it in st["items"] if it["id"] == item["id"]]
    save("q5", {"token": tok, "item": item["id"], "first_prompt": r1, "herdr_blocked": g1,
                "refused_prompt": r2, "screen_equal_before_after_refused": scr1 == scr2,
                "screen_before": scr1, "screen_after": scr2, "herdr_after_refused": g2,
                "hub_events_after_refused_prompt": evs_mid, "t_esc": t_esc, "status_after_esc": s3,
                "t_status_after_esc": t3, "herdr_after_esc": g3, "screen_after_esc": scr3,
                "hub_events": evs, "pollers_after": st["pollers"], "item_after": item_state,
                "typed_text_in_transcript": typed_in_transcript,
                "pass": (g1 or {}).get("agent_status") == "blocked" and r2["rc"] != 0 and "agent_blocked" in (r2["stderr"] + r2["stdout"])
                        and not typed_in_transcript and s3 in ("idle", "done")
                        and not [e for e in evs_mid if e.get("hook") == "UserPromptSubmit" or e.get("kind") == "inbox_pull"]})


def cmd_close():
    a = agent_get("close check")
    pane = my_pane()
    if a and a.get("pane_id") != pane:
        raise SystemExit(f"{NAME} is not in my pane; not closing anything")
    r = sh(["herdr", "pane", "close", pane], timeout=30, note="close")
    print(r["rc"], r["stdout"], r["stderr"])


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    c, rest = sys.argv[1], sys.argv[2:]
    if c == "split":
        cmd_split()
    elif c == "start":
        cmd_start()
    elif c == "get":
        print(json.dumps(guard(), indent=1))
    elif c == "list":
        print(sh(["herdr", "agent", "list"], timeout=20, note="list")["stdout"])
    elif c == "read":
        print(read_screen(rest[0] if rest else "visible", int(rest[1]) if len(rest) > 1 else 60))
    elif c == "keys":
        cmd_keys(rest)
    elif c in ("q1", "q2", "q3", "q4a", "q4b", "q5"):
        globals()["cmd_" + c]()
    elif c == "close":
        cmd_close()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
