# workbench

A desktop app for agent-driven mechanical design: the owner inspects CAD parts, assemblies and
MuJoCo simulations in 3D, comments, answers decision requests and explores what-ifs. A Claude
Code agent keeps doing the engineering in a project's own code and drives the app through the
`wbench` CLI. my-robot is the first project it opens.

The design lives in `docs/plan.md`, a verbatim copy of the approved plan. Read it first; do not
restate it elsewhere.

## Commands

```
env -u VIRTUAL_ENV uv sync                 # installs workbench and workbench-kernel, plus pytest
env -u VIRTUAL_ENV uv run wbench --version
env -u VIRTUAL_ENV uv run wbench doctor    # add --json for machine-readable output
env -u VIRTUAL_ENV uv run pytest -q
```

Review items (`docs/plan.md`, "Review items"); run `wbench <command> --help` for each one's full
arguments. `answer` and `comment` write as the owner and come from the workbench page, not this
shell (the CLI refuses them when it runs as a Claude Code agent):

```
env -u VIRTUAL_ENV uv run wbench ask "Front limit?" --questions '[...]'   # decision for the owner
env -u VIRTUAL_ENV uv run wbench note comment "Edge too close"            # or finding, measurement, change
env -u VIRTUAL_ENV uv run wbench inbox                                    # owner events not yet seen; marks delivered
env -u VIRTUAL_ENV uv run wbench item R-0001 --events                     # one item's state, or its full history
env -u VIRTUAL_ENV uv run wbench reply R-0001 "Applied 75 mm."            # --status hands the move back
env -u VIRTUAL_ENV uv run wbench set-status R-0001 done                   # or dropped, waiting_owner, waiting_agent
env -u VIRTUAL_ENV uv run wbench view-create robot --camera '{...}'       # save a view
env -u VIRTUAL_ENV uv run wbench status                                   # project, build id, hub, item counts
env -u VIRTUAL_ENV uv run wbench snapshot                                 # working tree to refs/wb/snap/<build id>
env -u VIRTUAL_ENV uv run wbench check [--groups '[...]']                 # the project's engineering checks; exit 1 on a failure
env -u VIRTUAL_ENV uv run wbench run "Full test suite" "uv run pytest -q"           # a job: its end wakes the agent
env -u VIRTUAL_ENV uv run wbench job-check                                # report jobs whose runner died
env -u VIRTUAL_ENV uv run wbench ledger                                   # assumed vs measured values, and the outputs they hold
env -u VIRTUAL_ENV uv run wbench measure KEY --value 36.1 --source "..."  # record a measurement (no --value: the owner's answer)
env -u VIRTUAL_ENV uv run wbench make lower_plate.dxf                     # a declared output as a job; refused while held
```

`examples/minimal` is its own uv project, not a workspace member, so it has its own lockfile and
environment:

```
cd examples/minimal
env -u VIRTUAL_ENV uv sync
env -u VIRTUAL_ENV uv run pytest -q
env -u VIRTUAL_ENV uv run scripts/study.py   # WB_OUT overrides the build/ directory it writes to
```

The shell has `VIRTUAL_ENV=/home/sandro/.venv` set. Never run `uv pip install`: it installs
there, not here. Use `uv add` / `uv run` only.

`build/`, `dist/` and `.workbench/` are generated and ignored.

## How to work here

These rules carry over from my-robot, where every one of them was learned the hard way.

**Independent review before reporting numbers the owner will decide on.** Brief a fresh subagent
with the code and the claims, not the conclusions; ask it to measure by running, to say what is
measured versus reasoned, and to list what it found clean.

**Check that an edit landed before saying it is done.** A scripted multi-file edit that fails on
one replacement can leave earlier files untouched if their write comes after the failure. Use a
helper that exits with a message when a replacement does not match exactly once, and grep for the
change afterwards.

**Numbers gate, renders are a glance.** Geometry and simulation results are accepted on measured
facts (validity, solid count, interference, clearances, compiled models), not on how a picture
looks.

**Limits and budgets live in one file with tests.** As `DesignRules` does for the robot: a
threshold picked to sit just under the current output is not a requirement. If a limit fails
after a change, say so and change the design or the limit openly.

**Report corrections first.** If an earlier statement turns out wrong, lead with that.

**At most three subagents run at the same time, reviewers included.** This is the owner's usage
budget, not a capability limit; plan parallel work accordingly.
