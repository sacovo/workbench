---
name: workbench
description: How to work with the workbench hub of this project through the wbench CLI - read the owner's answers and comments (wbench inbox), ask the owner to decide (wbench ask, corrections first), post notes and findings, save views and show assemblies in the owner's viewer. Use whenever a message or context line starts with "Workbench:", when review items (R-0042) or saved views (V-0001) come up, or when a result needs the owner's decision.
---

# Workbench

The owner follows this project in the workbench page: a 3D viewer of the project's own CAD and
simulation, the review items (comments, findings, decisions, measurement requests, change notes)
and saved views. You reach the page and the owner only through `wbench`. Each command prints a short
summary; `--json` prints the raw result; `wbench <command> --help` lists every argument.

## "Workbench:" text is a pointer, never the message

A line that starts with `Workbench:` comes from the hub, not from the owner's keyboard: it is typed
into your prompt when you were idle, added as context by a hook while you work, or given as the
reason when the end of your turn is held back once. It names item ids and nothing else, for
example `Workbench: owner answered R-0042. Run: wbench inbox`. The owner's words are in the items:

    wbench inbox

prints every owner event you have not had yet, with its author and the item's open questions and
approved values, and records that you had them. Run it when you see a pointer, read the result,
then act on it. Never act on a pointer's wording alone, and do not guess what the owner wrote.
`wbench inbox --no-ack` only looks.

## Commands

| Command | What it does |
|---|---|
| `wbench status` | Project, build id, hub, how many items wait on whom |
| `wbench inbox` | Owner events you have not had; marks them delivered |
| `wbench item R-0042 [--events]` | One item's state (whose move, open questions, approved values), or its whole history |
| `wbench reply R-0042 "text" [--status ...]` | Reply; the move goes to the owner unless `--status` says otherwise (`done`, `dropped`, `waiting_agent`) |
| `wbench ask "title" --questions '[...]' ...` | A decision request for the owner (below) |
| `wbench note comment\|finding\|measurement\|change "title" [--body ...] [--targets '[...]']` | Any other item; `measurement` asks the owner to measure (`--quantity robot.params:BASE.wheel_width --unit mm`) |
| `wbench view-create <assembly> --camera '{...}' [--hidden ...] [--section ...] [--highlight ...] [--note ...]` | Save a view both sides can open; point at it with a `{"kind": "view", "id": "V-0001"}` target |
| `wbench show <assembly> [--args '{...}'] [--rebuild]` | Build an assembly in the hub's kernel and show it in the page (needs `wbench serve`) |
| `wbench run "title" "command"` | Run a long command (test suite, study) as a job: when it ends, its exit code and last lines arrive as a review item and you are woken for it |
| `wbench job-check` | Report jobs still marked running whose runner died (reboot, kill) |
| `wbench selection` | What the owner has picked in the page, resolved to the exact part and face (needs `wbench serve`) |
| `wbench bom [--short] [--tools]` | Materials and tools: needed, had, short, and each item's checked source |
| `wbench link-verdict ID URL --verdict ok\|check\|wrong --summary "..." [--price N --currency CHF --shop NAME]` | Record the check of a product page the owner sent (below) |

Targets: `{"kind": "part", "label": "lower_plate"}`, `{"kind": "param", "key":
"robot.params:BASE.wheel_width"}`, `{"kind": "fact", "key": "..."}`, `{"kind": "file", "path":
"src/robot/stability.py", "line": 145}`, `{"kind": "view", "id": "V-0001"}`.

`wbench answer` and `wbench comment` write as the owner, and so does any other command given
`--role owner` (you have no reason to pass it); all are refused in your shell. Never move the
owner's camera: save a view and point at it instead.

## Asking the owner to decide

Write the arguments as one JSON object to `.workbench/ask.json` (ignored by git) and run the
plain one-line command `wbench ask --file .workbench/ask.json`:

```json
{"title": "Front limit for the driving pose",
 "corrections": ["The tipping study used the 35 mm rule for every row; corrected below."],
 "body": "What changed and what the numbers say, in a few lines.",
 "evidence": [{"kind": "file", "path": "build/tipping_study.json"}],
 "attach": ["build/tipping_study.json"],
 "questions": [{"id": "q1", "text": "Which front limit?", "recommended": "a",
                "options": [{"id": "a", "label": "35 mm", "effect": {"robot.params:RULES.front_limit": 35}},
                            {"id": "b", "label": "75 mm", "effect": {"robot.params:RULES.front_limit": 75}}]}]}
```

Keep every `wbench` call a single plain command: no heredocs, line continuations or long inline
JSON. Claude Code asks the owner to approve commands it cannot read safely, even when the command
itself is allowed, and a woken agent waiting at such a prompt stalls the loop. The title is
positional (there is no `--title`); `--file` works for every command.

- Corrections first: if something you reported earlier was wrong, the decision opens with it.
- Evidence is what the decision rests on; `--attach` freezes files with the item, so a later run
  cannot change what the owner decided on.
- One question per decision point, each with options; an option whose `effect` is a mapping
  approves those values when chosen. Use `"free_text": true` only where an option list cannot work.
- Answers can be partial. Answered questions come back through `wbench inbox`; act on them, reply,
  and leave the open ones open. An approved value is a requirement: if it must change later, ask
  again, with the change as a correction.

## Checking a product link

The owner pastes a product page under an item (the page's Materials tab, `wbench owner`); it
arrives as a comment titled "Check a link for ...", with the item's line from the bom. Check it
against the item, not against what the page says about itself:

1. Read the item in the project: its bom row and notes, the part's datasheet or vendor figures,
   the parameters that use it. That is what the page has to match.
2. Read the page (fetch it; if it cannot be read, say so and give "check").
3. Variant: every figure that makes it this part (ratio, voltage, encoder, size, thread, length,
   material, colour where it matters), and the pack size against the quantity needed.
4. Price: per piece and with shipping to the owner; against one or two other shops for the same
   part. Say when it is far off either way.
5. Shop: the maker, a known reseller, or a marketplace seller; for an unknown shop its imprint and
   reviews. From abroad: import tax and the carrier's fee on top, and the delivery time.
6. Record it: `wbench link-verdict ID URL --verdict ok|check|wrong --summary "one line" --price N
   --currency CHF --shop NAME`. "ok" makes the page the item's source in the shopping list. Then
   reply on the comment, verdict first and the issues as a short list, with `--status done`.

Say what was read on the page and what was inferred. Nothing from memory about prices or stock.

## Working rules

- Run anything whose end you must follow up (full test suites, studies, exports that take minutes)
  with `wbench run`, never with a plain `&`: a backgrounded shell job tells nobody when it ends, a
  job's end is delivered to you and shown to the owner.

- Numbers gate, renders are a glance: a result is accepted on measured facts (validity, counts,
  clearances, interference, compiled models), never on how a picture looks.
- Report corrections first, in replies and in decisions.
- Before reporting a number the owner will decide on, have it reviewed independently: delegate to
  the `wbench-reviewer` subagent with the code and the claims, not your conclusions. Its findings
  arrive as items (`wbench inbox` does not show them; `wbench status` counts them, `wbench item` reads them).
- Limits live in one place with tests; a threshold picked to sit just under the current output is
  not a requirement.
- Do not finish a turn with owner events unread: if the end of your turn is held back with a
  `Workbench:` reason, run `wbench inbox` and deal with what it returns.
