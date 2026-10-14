# S2: agent channels (hooks and herdr), result

Run on 2026-10-14 against Claude Code 2.1.283 (not upgraded), herdr 0.9.0 in kitty, model
`claude-haiku-4-5-20251001`. One live agent `s2probe` in pane `wC:p4` (split right of the caller,
no focus), cwd `examples/minimal`, hooks loaded with `--settings settings.json`. Seven agent turns;
Claude Code's own accounting gives 0.111 USD at the last turn end (debug log) and 0.117 USD for
the session (`lastCost` in `~/.claude.json`). Not yet re-measured by an independent reviewer.

**Correction during the run.** The driver first marked Q4a (asyncRewake wake) as failed. Its
pass rule assumed that a rewake turn fires no `UserPromptSubmit` hook. It does fire it
(measured), so the rule was wrong, not the wake. The rule now checks that nothing was typed
(driver log) and that the turn's user entry is a `<task-notification>` (transcript). The original
verdict is kept in `run/q4a.json` (`pass_original_criterion: false`).

## S2 criteria

| Criterion (plan) | Result | Basis |
|---|---|---|
| Mid-turn context arrives after the next tool call | PASS | measured |
| Stop hook blocks once | PASS | measured |
| Idle agent is woken | PASS, both by asyncRewake and by `herdr agent prompt` | measured |
| Nothing is sent to a blocked agent | PASS | measured |
| Hook overhead p95 under 150 ms | PASS: worst p95 22.5 ms with the hub up or down; 122.3 ms with a hung hub | measured |

The plan's fallback ("prompt-time pull plus herdr wake only") is not needed.

## Questions

**1. `UserPromptSubmit` with `additionalContext`** (measured). I added item R-0001 holding the
random token `WB-1DBEA6`, then ran `herdr agent prompt s2probe "Repeat verbatim any workbench note
you were given with this message." --wait`. The hook took the item from the hub. The transcript
holds an `attachment` of type `hook_additional_context` (hookEvent `UserPromptSubmit`) with the
token, and the reply was "Owner note: the release token is WB-1DBEA6." The debug log says
"provided additionalContext (149 chars)". The turn took 3.5 s.

**2. `PostToolUse` mid-turn** (measured). Prompt: "Run the Bash command `sleep 8`. When it has
finished, reply with any workbench note you received, verbatim."

| Event | Time (UTC) |
|---|---|
| `sleep 8` tool call issued | 06:37:38.216 |
| Item R-0002 (`WB-45F263`) added at the hub | 06:37:39.474, 1.26 s into the sleep |
| Tool result, PostToolUse delivers R-0002, `hook_additional_context` in transcript | 06:37:46.52 |
| Final reply quoting the token | 06:37:48.691 |

The turn had one `UserPromptSubmit` and one Stop, so the item reached the agent in the same turn.
Mid-turn `herdr agent get` said `working` and `claude agents --json` said `busy`.

**3. `Stop` block once, no loop** (measured). Two items were armed: R-0003 to appear right after
the prompt hook, so only the Stop hook could catch it, and R-0004 to appear right after the next
PostToolUse call. Prompt: "Reply with just the word OK."

- The agent said "OK". Stop #1 came with `stop_hook_active=false` and R-0003 unread, so the hook
  returned `{"decision":"block","reason":"Workbench: owner item R-0003 is unread. Run exactly: curl
  -s http://127.0.0.1:47617/inbox ..."}`.
- The transcript shows one `Stop hook feedback: ...` user entry and one `hook_blocking_error`
  attachment.
- The agent ran the curl (allowed by `settings.json`, no prompt) and pulled R-0003. The
  PostToolUse call made R-0004 appear, and the agent quoted R-0003's token.
- Stop #2 came with `stop_hook_active=true` and R-0004 unread. It was allowed and the turn ended:
  two stop-hook summaries, no third Stop, herdr `done`.
- herdr stayed `working` between the two stops, with no flicker to idle.

**4a. asyncRewake from idle** (measured). The rewake poller is a Stop hook with
`"asyncRewake": true, "timeout": 3600`. It long-polls the hub and exits 2 on a new owner item.
After the agent had been `done` for 20 s, I added R-0005. The timeline, measured from the moment
the item was added:

| Event | After the item |
|---|---|
| Poller returned `wake` and exited 2 | 0 ms |
| Transcript: new user entry `<task-notification><summary>workbench item</summary></task-notification><system-reminder>Workbench: Owner answered R-0005. Run: curl -s .../inbox</system-reminder>` | 19 ms |
| herdr `working` by `agent get` polled every 50 ms | 261 ms |
| herdr push `working` | 275 ms |

The agent ran the curl and quoted R-0005. Nothing was typed in that window (driver log). Two
behaviours of this turn matter for the design:

- `UserPromptSubmit` fired on it and delivered R-0004 and R-0005 as context, so the curl came back
  empty.
- Its Stop arrived with `stop_hook_active=true`.

**4b. `herdr agent prompt` wake** (measured). Rewake was disabled at the hub, and I added R-0006.

- Before: `herdr agent get` showed `agent_status: done`, `state_change_seq: 1393`.
- Command: `herdr agent prompt s2probe "Owner answered R-0006. Run: curl -s
  http://127.0.0.1:47617/inbox" --wait --timeout 120000`. It returned rc 0 after 4.21 s with
  `type: agent_prompted` and status `done`.
- After: `done`, `state_change_seq: 1395`. `working` was never visible in the before and after
  snapshots; the push subscription saw it 251 ms after the prompt hook.
- The prompt hook delivered R-0006, the agent ran the curl (empty) and quoted the token.

**5. Blocked agent** (measured).

- `herdr agent prompt s2probe "Run this exact Bash command: python3 -c 'print(6*7)'" --wait`
  returned after 2.2 s with status `blocked`. The screen showed "This command requires approval …
  1. Yes 2. Yes, and don't ask again for: python3 * 3. No".
- I added R-0007, then ran `herdr agent prompt s2probe "Owner answered R-0007. Run: curl -s
  .../inbox"`. It returned rc 1 in 1 ms with `{"error":{"code":"agent_blocked","message":"agent
  s2probe is blocked and requires interactive input"}}`.
- Nothing arrived: no hook call at the hub, `state_change_seq` unchanged, and the typed text is
  absent from the transcript. The screen 2 s later differed only in the animated status glyph.
- `herdr agent send-keys s2probe esc` (one key) denied the tool ("Bash tool permission denied"),
  and python never ran. The agent was `done` 56 ms later by `agent get` (112 ms by push), with an
  empty prompt box.
- R-0007 stayed unread and was delivered by `UserPromptSubmit` on the next prompt.

**6. Observation** (measured).

- `herdr agent list` and `get` give the same fields: `agent`, `agent_session` (`value` = Claude
  session id, reported by herdr's own SessionStart hook), `agent_status`, `cwd`, `foreground_cwd`,
  `focused`, `interactive_ready`, `name`, `pane_id`, `tab_id`, `workspace_id`, `terminal_id`,
  `terminal_title(_stripped)`, `revision`, `state_change_seq`. There is no transcript path.
- Statuses seen: `unknown` (shell), `blocked` (trust dialog, permission prompt), `idle` (after
  startup), `working`, and `done` after every turn, because the pane is never focused.
- herdr has a push channel: on `$HERDR_SOCKET_PATH`, send `{"method":"events.subscribe","params":
  {"subscriptions":[{"type":"pane.agent_status_changed","pane_id":"wC:p4"}]}}`. It answers
  `subscription_started`, then streams `{"event":"pane.agent_status_changed","data":{"agent_status":
  ...}}`.
- Latencies against Claude-side events:

| Transition | Push (n, median, range) | Polling every 200 ms (median, range) |
|---|---|---|
| `working` after the prompt hook | n=7, 165 ms, 87–251 ms | 218 ms, 100–392 ms |
| `done` after the final Stop | n=6, 304 ms, 166–343 ms | 306 ms, 174–478 ms |
| `blocked` after Claude's permission request | 266 ms | — |
| `done` after Esc | 112 ms | — |

- `claude agents --json` exists on 2.1.283. It takes 0.14 s and returns `{pid, cwd, kind:
  "interactive", startedAt, sessionId, name, status}`. Statuses seen: `idle`, `busy`, and
  `waiting` during the permission prompt. It has no pane id; join to herdr by `sessionId`.

**7. Hook overhead** (measured). `bench_hooks.py bench -n 40`: each hook timed as a subprocess
(`/usr/bin/python3 -I -S`, Python 3.14.7) with the hook JSON recorded in the live run on stdin.
Notification used synthetic input because it never fired live. Hub up is a private hub on 47630,
hub down a closed port, and hub hung a socket that accepts but never answers. 0 unexpected
outputs in 21 cells. All values in ms.

| Hook | Hub up, empty p50/p95 | Hub up, one item p50/p95 | Hub down p50/p95 (max) | Hub hung p50/p95 |
|---|---|---|---|---|
| UserPromptSubmit | 18.6 / 19.8 | 18.7 / 20.2 | 12.5 / 12.9 (13.3) | 120.4 / 121.4 |
| PostToolUse | 19.6 / 21.3 | 19.6 / 21.4 | 13.0 / 13.9 (14.0) | 121.0 / 122.3 |
| Stop, active false | 20.0 / 22.2 | 19.6 / 20.8 | 13.6 / 14.9 (16.9) | 120.7 / 121.7 |
| Stop, active true | 19.1 / 20.3 | 18.9 / 19.8 | 12.5 / 13.0 (13.3) | 120.5 / 122.0 |
| Notification | 19.1 / 20.1 | — | 12.7 / 13.1 (13.2) | 120.5 / 122.1 |
| Rewake poller | — | — | 12.7 / 13.5 (14.0) | — |

Through `zsh -c`, as Claude Code runs hooks, UserPromptSubmit took 20.9 / 22.5 ms (about +2 ms).
With the hub down every hook exits 0, prints nothing and takes 17 ms at most, against the 50 ms
limit. The hung case is bounded by the 100 ms request timeout and is the only one near 150 ms.
`bench_hooks.py check` passes 19 of 19 functional checks.

## Surprises

- The asyncRewake wake works from idle on 2.1.283, but it has three gaps (measured):
  - A rewake turn's Stop has `stop_hook_active=true`, so the stop hook cannot block in it.
  - An Esc-interrupted turn runs no Stop hook, so no poller is re-armed until the next turn.
  - Claude Code kills the detached poller when it exits (gone within 0.5 s of closing the pane),
    but the hub's long-poll is not told and keeps a stale registration.
- A rewake turn passes through `UserPromptSubmit` (measured).
- No `Notification` hook fired during 2.3 s at a permission prompt (measured). Claude Code did run
  its PermissionRequest hook path at once (`executePermissionRequestHooks` in the debug log); a
  `PermissionRequest` hook was not configured, so whether it would tell the hub is unmeasured.
- After any turn, herdr reports `done`, not `idle`, for an unfocused pane, and `agent get` before
  and after a turn look the same. Only `state_change_seq` or the push stream shows the turn
  (measured).
- Double delivery (measured): when a pointer is typed, the prompt hook has already injected the
  content, so the agent's pull returns empty. This conflicts with "content is pulled, which
  confirms delivery" (reasoned).
- The owner's terminal shows a normal stop block as "Stop hook error: …" (measured, cosmetic).
- `rewakeMessage` and `rewakeSummary` are marked `@internal` in the settings schema but work
  (measured). Claude Code overrides a stop hook after 8 consecutive blocks
  (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, read from the binary, not measured). The default hook
  timeout is 600 s, so a rewake poller needs an explicit long `timeout` (read from the binary).
- `import urllib.request` alone costs 40 ms in Python 3.14, so the hooks speak HTTP over a raw
  socket (measured).
- First start in a new folder blocks on the trust dialog: `agent start` returned `agent_not_ready`,
  and Down, Enter answered it (measured).

## What the Phase 1 agent transport should use (reasoned from the above)

1. Delivery while working: `UserPromptSubmit` and `PostToolUse` hooks returning
   `hookSpecificOutput.additionalContext` with item id, author and text. The hub marks an item
   delivered when a hook takes it. Tests confirm arrival through the transcript's
   `hook_additional_context` attachment.
2. Turn end: a Stop hook that blocks only when `stop_hook_active` is false and owner items are
   unread, with the fixed pointer ("Run wb inbox") as reason.
3. Idle wake: `herdr agent prompt <name> "<fixed pointer>" --wait`, sent only when the push stream
   says `idle` or `done`. herdr refuses a blocked agent by itself; the hub records the refusal and
   waits for the next state change, never retrying blindly. Keep asyncRewake out of Phase 1, at
   most as a herdr-less fallback, because of the gaps above and the internal fields.
4. State: herdr `events.subscribe` (`pane.agent_status_changed`) as the source; `herdr agent get`
   for pane and session-id snapshots; `claude agents --json` as a herdr-independent cross-check
   (it also shows `waiting`). Never infer a turn from two `get` snapshots.
5. Decide whether hooks inject content or only pointers, so that a typed pointer and the prompt
   hook do not deliver the same item twice.
6. Add a `PermissionRequest` hook so the hub learns about a blocked agent without herdr; this
   needs a measurement first.
7. Hook client as in this spike: `/usr/bin/python3 -I -S`, raw socket, connect timeout 50 ms,
   request timeout 100 ms or less, no output when the hub is down. The 150 ms overhead budget
   belongs in the budgets file with a test.

## What I ran (commands in order; every herdr and claude call is in `run/driver-log.jsonl`)

```
python3 bench_hooks.py check                                  # offline, 19/19
python3 hub.py --port 47617 &                                 # fake hub, events -> run/hub-events.jsonl
python3 driver.py split                                       # herdr pane split --current --direction right --cwd examples/minimal --no-focus -> wC:p4
python3 herdr_watch.py --pane wC:p4 --cwd .../examples/minimal --out run/herdr-watch.jsonl &
python3 driver.py start                                       # herdr agent start s2probe --kind claude --pane wC:p4 -- --model haiku --settings settings.json --strict-mcp-config --debug-file run/claude-debug.log
python3 driver.py read; python3 driver.py keys down enter     # trust dialog, my pane only
python3 driver.py q1 | q2 | q3 | q4a | q4b | q5               # one turn each
# one extra "Reply with just the word OK." turn (rewake re-enabled) to arm a poller -> run/q6-arm-before-close.json
python3 driver.py list; python3 driver.py close               # herdr pane close wC:p4
python3 bench_hooks.py bench -n 40                            # overhead -> run/bench.json
python3 summarize.py                                          # -> results.json
```

Before every send, `driver.py` checks that `s2probe` resolves to `wC:p4`. Before closing,
`claude` (pid 398975) and a live rewake poller (pid 415056, in its own session) were running;
both were gone 0.5 s after the close. Afterwards `herdr agent list` no longer showed the agent,
`pgrep` found no hub, watcher, hook or claude process from the spike, and no spike port was
listening.

## Changes outside this directory

- `~/.claude.json`: accepting the trust dialog in my pane made Claude Code record
  `hasTrustDialogAccepted: true` for `/home/sandro/work/privat/workbench` (the repository root, so
  new sessions anywhere in the repo skip the dialog), plus `lastSessionId` and `lastCost`. I did
  not edit the file.
- `~/.claude/projects/-home-sandro-work-privat-workbench-examples-minimal/d685955c-….jsonl`: the
  probe's transcript.
- Nothing in `examples/minimal`, my-robot, `~/.claude/settings.json` or herdr's config. Nothing
  committed.

## Files

`hub.py` (fake hub), `hooks/` (UserPromptSubmit, PostToolUse, Stop, rewake poller,
Notification, shared client), `settings.json`, `driver.py`, `herdr_watch.py`, `bench_hooks.py`,
`summarize.py`, `results.json`. Under `run/`:

- `q*.json`: per-question evidence; `q*-transcript.json`: transcript slices.
- `hub-events.jsonl`, `herdr-watch.jsonl`, `driver-log.jsonl`.
- `claude-debug.log`.
- `recorded/`: the live hook inputs.
- `bench.json`, `check.json`.
