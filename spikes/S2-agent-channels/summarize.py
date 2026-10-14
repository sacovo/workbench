#!/usr/bin/env python3
"""Build run/../results.json from the saved run: per-question evidence, S2 criteria, latencies.

Reads run/q*.json, run/hub-events.jsonl, run/herdr-watch.jsonl, run/driver-log.jsonl,
run/claude-debug.log, run/bench.json and run/check.json. Writes results.json next to this file.
No agent, no network except nothing: pure post-processing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
RUN = SPIKE / "run"


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def j(p: Path):
    return json.loads(p.read_text())


def iso_to_epoch(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> None:
    hub = jl(RUN / "hub-events.jsonl")
    watch = jl(RUN / "herdr-watch.jsonl")
    q = {k: j(RUN / f"{k}.json") for k in ("q1", "q2", "q3", "q4a", "q4b", "q5")}
    bench = j(RUN / "bench.json")
    check = j(RUN / "check.json")
    debug = (RUN / "claude-debug.log").read_text().splitlines()

    push = [(w["ts"], w["msg"]["data"].get("agent_status")) for w in watch
            if w["src"] == "push" and "data" in w.get("msg", {})]
    poll = [(w["ts"], w.get("status")) for w in watch if w["src"] == "poll" and w.get("status")]
    cagents = [(w["ts"], [e.get("status") for e in w.get("entries", [])]) for w in watch if w["src"] == "claude_agents"]

    def first_after(series, t, states, within=10.0):
        for ts, s in series:
            if t - 0.5 <= ts <= t + within and s in states:
                return ts
        return None

    # turn starts: every UserPromptSubmit hook call; turn ends: Stop calls followed by herdr done/idle
    ups = [e for e in hub if e.get("hook") == "UserPromptSubmit"]
    stops = [e for e in hub if e.get("hook") == "Stop"]
    starts = []
    for e in ups:
        tp = first_after(push, e["ts"], {"working"})
        tq = first_after(poll, e["ts"], {"working"})
        starts.append({"hook_ts": e["ts"], "push_working_ms": tp and round((tp - e["ts"]) * 1000),
                       "poll_working_ms": tq and round((tq - e["ts"]) * 1000)})
    ends = []
    for ts, s in push:
        if s not in ("done", "idle"):
            continue
        prior = [x for x in stops if x["ts"] <= ts and ts - x["ts"] < 5]
        if prior:
            st = prior[-1]
            tq = first_after(poll, st["ts"], {"done", "idle"})
            ends.append({"stop_ts": st["ts"], "stop_hook_active": st["stop_hook_active"],
                         "push_done_ms": round((ts - st["ts"]) * 1000),
                         "poll_done_ms": tq and round((tq - st["ts"]) * 1000)})
    # blocked: Claude Code's permission request (debug log) -> herdr blocked
    perm = [ln for ln in debug if "executePermissionRequestHooks called" in ln]
    t_perm = iso_to_epoch(perm[0].split(" ")[0]) if perm else None
    blocked = {"permission_request_ts": t_perm,
               "push_blocked_ms": t_perm and round((first_after(push, t_perm, {"blocked"}) - t_perm) * 1000),
               "poll_blocked_ms": t_perm and round((first_after(poll, t_perm, {"blocked"}) - t_perm) * 1000),
               "claude_agents_waiting_seen": any("waiting" in s for _, s in cagents)}
    t_esc = q["q5"]["t_esc"]
    esc = {"push_done_ms": round((first_after(push, t_esc, {"done", "idle"}) - t_esc) * 1000),
           "poll_done_ms": round((first_after(poll, t_esc, {"done", "idle"}) - t_esc) * 1000)}

    def dist(xs):
        xs = [x for x in xs if x is not None]
        return {"n": len(xs), "min": min(xs), "median": statistics.median(xs), "max": max(xs)} if xs else None

    # usage reported by Claude Code at each turn end (cumulative for the session)
    turns = [ln for ln in debug if re.search(r"\[engine\] turn \d+ end", ln)]
    cost = re.search(r"cost=\$([0-9.]+)", turns[-1]).group(1) if turns else None

    rows = bench["rows"]
    worst_p95 = max(r["p95_ms"] for r in rows if r["scenario"] != "hung")
    worst_p95_hung = max(r["p95_ms"] for r in rows if r["scenario"] == "hung")
    worst_down_max = max(r["max_ms"] for r in rows if r["scenario"] == "down")

    q2, q3, q4a, q4b, q5 = q["q2"], q["q3"], q["q4a"], q["q4b"], q["q5"]
    q2_lines = j(RUN / "q2-transcript.json")
    tool_use = [x for x in q2_lines if x["type"] == "assistant" and "tool_use" in x["kinds"]]
    tool_res = [x for x in q2_lines if x["type"] == "user" and "tool_result" in x["kinds"]]
    q2_t_tool_use = iso_to_epoch(tool_use[0]["ts"]) if tool_use else None
    q2_t_tool_res = iso_to_epoch(tool_res[0]["ts"]) if tool_res else None
    # Claude-side record of the stop block, independent of the hub log
    q3_lines = j(RUN / "q3-transcript.json")
    q3_block_attachments = sum(1 for x in q3_lines if "attachment:hook_blocking_error" in x["kinds"])
    q3_feedback_entries = sum(1 for x in q3_lines if x["type"] == "user" and x["text"].startswith("Stop hook feedback"))
    q3_stop_summaries = sum(1 for x in q3_lines if "system:stop_hook_summary" in x["kinds"])

    criteria = [
        {"id": "mid_turn_context_after_next_tool_call", "pass": bool(q2["pass"]
                                                                     and q2_t_tool_use and q2_t_tool_use < q2["t_add"] < q2_t_tool_res),
         "basis": "measured",
         "evidence": {"item_added_s_after_tool_start": round(q2["t_add"] - q2_t_tool_use, 3),
                      "tool_result_s_after_item": round(q2_t_tool_res - q2["t_add"], 3),
                      "delivered_by": "PostToolUse", "user_prompt_submit_calls_in_turn": q2["n_user_prompt_submit"],
                      "token_in_final_reply": any(x["type"] == "assistant" for x in q2["token_lines"])}},
        {"id": "stop_hook_blocks_once", "pass": bool(q3["pass"] and q3_block_attachments == 1 and q3_feedback_entries == 1
                                                     and q3_stop_summaries == 2), "basis": "measured",
         "evidence": {"stops": [{"stop_hook_active": s["stop_hook_active"], "unread": s["unread"]} for s in q3["stops"]],
                      "transcript_hook_blocking_error_attachments": q3_block_attachments,
                      "transcript_stop_hook_feedback_entries": q3_feedback_entries,
                      "transcript_stop_hook_summaries": q3_stop_summaries,
                      "inbox_pulls": len(q3["inbox_pulls"]), "unread_after_turn": q3["tok_b_unread_after"],
                      "status_after": q3["status_after"]}},
        {"id": "idle_agent_is_woken", "pass": bool(q4a["pass"] and q4b["pass"]), "basis": "measured",
         "evidence": {"asyncRewake": {"poller": q4a["poller_result"]["action"],
                                      "herdr_working_ms_after_item_agent_get_50ms": round((q4a["t_working"] - q4a["t_add"]) * 1000),
                                      "herdr_working_ms_after_item_push": round((first_after(push, q4a["t_add"], {"working"}) - q4a["t_add"]) * 1000),
                                      "transcript_turn_start_ms_after_item": round(
                                          (iso_to_epoch(q4a["task_notification_lines"][0]["ts"]) - q4a["t_add"]) * 1000),
                                      "prompts_typed_in_window": len(q4a["driver_prompts_in_window"]),
                                      "pass_original_criterion": q4a.get("pass_original_criterion"),
                                      "criterion_note": q4a.get("criterion_note")},
                      "herdr_prompt": {"rc": q4b["prompt"]["rc"], "wait_s": q4b["prompt"]["dur_s"],
                                       "before": q4b["herdr_before"]["agent_status"],
                                       "after": q4b["herdr_after"]["agent_status"],
                                       "state_change_seq": [q4b["herdr_before"]["state_change_seq"],
                                                            q4b["herdr_after"]["state_change_seq"]]}}},
        {"id": "nothing_sent_to_blocked_agent", "pass": bool(q5["pass"]), "basis": "measured",
         "evidence": {"herdr_status": q5["herdr_blocked"]["agent_status"], "refused_rc": q5["refused_prompt"]["rc"],
                      "refused_stderr": q5["refused_prompt"]["stderr"].strip(),
                      "refused_in_s": q5["refused_prompt"]["dur_s"],
                      "hook_calls_after_refusal": [e for e in q5["hub_events_after_refused_prompt"]],
                      "typed_text_in_transcript": len(q5["typed_text_in_transcript"]),
                      "screen_diff": "only the animated status glyph on the pending tool line",
                      "cleared_with": "one Esc", "status_after_esc": q5["status_after_esc"]}},
        {"id": "hook_overhead_p95_under_150ms", "pass": worst_p95 < 150 and worst_p95_hung < 150, "basis": "measured",
         "limit_ms": 150, "evidence": {"worst_p95_ms_hub_up_or_down": worst_p95, "worst_p95_ms_hub_hung": worst_p95_hung,
                                       "worst_max_ms_hub_down": worst_down_max, "hub_down_limit_ms": 50,
                                       "hub_down_pass": worst_down_max < 50}},
    ]

    results = {
        "spike": "S2 agent channels",
        "run_date": dt.datetime.fromtimestamp(hub[0]["ts"], dt.UTC).isoformat(),
        "versions": {"claude_code": "2.1.283", "herdr": "0.9.0", "model": "claude-haiku-4-5-20251001",
                     "hook_python": bench["meta"]["python"]},
        "agent": {"name": "s2probe", "pane": "wC:p4", "session": q4a["herdr_before"]["agent_session"]["value"],
                  "engine_turns": len(turns), "session_cost_usd_reported_by_claude_code": cost,
                  "launch": "herdr agent start s2probe --kind claude --pane wC:p4 -- --model haiku --settings settings.json "
                            "--strict-mcp-config --debug-file run/claude-debug.log"},
        "criteria": criteria,
        "questions": {
            "q1_user_prompt_submit": {"token": q["q1"]["token"], "pass": q["q1"]["pass"],
                                      "transcript_kinds": [x["kinds"] for x in q["q1"]["token_lines"]]},
            "q2_post_tool_use": {"token": q2["token"], "pass": q2["pass"], "t_tool_use": q2_t_tool_use,
                                 "t_item_added": q2["t_add"], "t_tool_result": q2_t_tool_res},
            "q3_stop": {"pass": q3["pass"], "stops": len(q3["stops"])},
            "q4a_async_rewake": {"pass": q4a["pass"], "poller": q4a["poller_result"],
                                 "user_prompt_submit_fired_on_rewake": q4a["n_user_prompt_submit"] > 0,
                                 "stop_hook_active_on_rewake_turn_stop": [e.get("stop_hook_active") for e in q4a["hub_events"]
                                                                          if e.get("hook") == "Stop"]},
            "q4b_herdr_prompt": {"pass": q4b["pass"], "typed": q4b["typed"]},
            "q5_blocked": {"pass": q5["pass"], "notification_hook_fired": any(e.get("hook") == "Notification" for e in hub),
                           "stop_hook_after_esc": any(e.get("hook") == "Stop" and e["ts"] > t_esc and e["ts"] < t_esc + 30 for e in hub),
                           "item_left_unread_until_next_prompt": q5["item"]},
        },
        "observation": {"turn_start_latency_ms": starts, "turn_end_latency_ms": ends,
                        "turn_start_push_ms": dist([s["push_working_ms"] for s in starts]),
                        "turn_end_push_ms": dist([e["push_done_ms"] for e in ends]),
                        "turn_start_poll_ms": dist([s["poll_working_ms"] for s in starts]),
                        "turn_end_poll_ms": dist([e["poll_done_ms"] for e in ends]),
                        "blocked": blocked, "esc_to_done": esc,
                        "herdr_agent_fields": sorted(j(RUN / "q6-agent-get.json")["result"]["agent"].keys()),
                        "claude_agents_json": j(RUN / "q6-claude-agents.json"),
                        "claude_agents_statuses_seen": sorted({s for _, ss in cagents for s in ss if s})},
        "overhead": {"meta": bench["meta"], "rows": [{k: r[k] for k in ("hook", "scenario", "input", "n", "p50_ms", "p95_ms", "max_ms",
                                                                        "n_unexpected")} for r in rows]},
        "offline_checks": {"n": check["n_checks"], "failed": check["n_failed"]},
    }
    (SPIKE / "results.json").write_text(json.dumps(results, indent=1, default=str))
    for c in criteria:
        print(("PASS" if c["pass"] else "FAIL"), c["id"])
    print(json.dumps(results["observation"], indent=1, default=str)[:3000])
    print("engine turns", len(turns), "cost", cost)


if __name__ == "__main__":
    main()
