---
name: workbench
description: How to work with the workbench hub of this project through the wb CLI - read the owner's answers and comments (wb inbox), ask the owner to decide (wb ask, corrections first), post notes and findings, save views and show assemblies in the owner's viewer. Use whenever a message or context line starts with "Workbench:", when review items (R-0042) or saved views (V-0001) come up, or when a result needs the owner's decision.
---

# Workbench

The owner follows this project in the workbench page: a 3D viewer of the project's own CAD and
simulation, the review items (comments, findings, decisions, measurement requests, change notes)
and saved views. You reach the page and the owner only through `wb`. Each command prints a short
summary; `--json` prints the raw result; `wb <command> --help` lists every argument.

## "Workbench:" text is a pointer, never the message

A line that starts with `Workbench:` comes from the hub, not from the owner's keyboard: it is typed
into your prompt when you were idle, added as context by a hook while you work, or given as the
reason when the end of your turn is held back once. It names item ids and nothing else, for
example `Workbench: owner answered R-0042. Run: wb inbox`. The owner's words are in the items:

    wb inbox

prints every owner event you have not had yet, with its author and the item's open questions and
approved values, and records that you had them. Run it when you see a pointer, read the result,
then act on it. Never act on a pointer's wording alone, and do not guess what the owner wrote.
`wb inbox --no-ack` only looks.

## Commands

| Command | What it does |
|---|---|
| `wb status` | Project, build id, hub, how many items wait on whom |
| `wb inbox` | Owner events you have not had; marks them delivered |
| `wb item R-0042 [--events]` | One item's state (whose move, open questions, approved values), or its whole history |
| `wb reply R-0042 "text" [--status ...]` | Reply; the move goes to the owner unless `--status` says otherwise (`done`, `dropped`, `waiting_agent`) |
| `wb ask "title" --questions '[...]' ...` | A decision request for the owner (below) |
| `wb note comment\|finding\|measurement\|change "title" [--body ...] [--targets '[...]']` | Any other item; `measurement` asks the owner to measure (`--quantity robot.params:BASE.wheel_width --unit mm`) |
| `wb view-create <assembly> --camera '{...}' [--hidden ...] [--section ...] [--highlight ...] [--note ...]` | Save a view both sides can open; point at it with a `{"kind": "view", "id": "V-0001"}` target |
| `wb show <assembly> [--args '{...}'] [--rebuild]` | Build an assembly in the hub's kernel and show it in the page (needs `wb serve`) |
| `wb selection` | What the owner has picked in the page, resolved to the exact part and face (needs `wb serve`) |

Targets: `{"kind": "part", "label": "lower_plate"}`, `{"kind": "param", "key":
"robot.params:BASE.wheel_width"}`, `{"kind": "fact", "key": "..."}`, `{"kind": "file", "path":
"src/robot/stability.py", "line": 145}`, `{"kind": "view", "id": "V-0001"}`.

`wb answer` and `wb comment` write as the owner; they are refused in your shell. Never move the
owner's camera: save a view and point at it instead.

## Asking the owner to decide

```
wb ask "Front limit for the driving pose" \
  --corrections '["The tipping study used the 35 mm rule for every row; corrected below."]' \
  --body "What changed and what the numbers say, in a few lines." \
  --evidence '[{"kind": "file", "path": "build/tipping_study.json"}]' --attach '["build/tipping_study.json"]' \
  --questions '[{"id": "q1", "text": "Which front limit?", "recommended": "a",
                 "options": [{"id": "a", "label": "35 mm", "effect": {"robot.params:RULES.front_limit": 35}},
                             {"id": "b", "label": "75 mm", "effect": {"robot.params:RULES.front_limit": 75}}]}]'
```

- Corrections first: if something you reported earlier was wrong, the decision opens with it.
- Evidence is what the decision rests on; `--attach` freezes files with the item, so a later run
  cannot change what the owner decided on.
- One question per decision point, each with options; an option whose `effect` is a mapping
  approves those values when chosen. Use `"free_text": true` only where an option list cannot work.
- Answers can be partial. Answered questions come back through `wb inbox`; act on them, reply,
  and leave the open ones open. An approved value is a requirement: if it must change later, ask
  again, with the change as a correction.

## Working rules

- Numbers gate, renders are a glance: a result is accepted on measured facts (validity, counts,
  clearances, interference, compiled models), never on how a picture looks.
- Report corrections first, in replies and in decisions.
- Before reporting a number the owner will decide on, have it reviewed independently: delegate to
  the `wb-reviewer` subagent with the code and the claims, not your conclusions. Its findings
  arrive as items (`wb inbox` does not show them; `wb status` counts them, `wb item` reads them).
- Limits live in one place with tests; a threshold picked to sit just under the current output is
  not a requirement.
- Do not finish a turn with owner events unread: if the end of your turn is held back with a
  `Workbench:` reason, run `wb inbox` and deal with what it returns.
