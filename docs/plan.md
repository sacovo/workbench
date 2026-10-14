# Workbench: a desktop app for agent-driven mechanical design

## Context

The robot so far was designed entirely in a terminal. Claude Code wrote build123d parts, generated
the MuJoCo model from the same parameters, ran studies and exports, and had fresh subagents review
every decision-driving result. That produced a tested design, but the transcript of that week
shows where the loop is weak:

- The owner never used an interactive viewer. He was pointed at files in `build/` five times,
  his one remark on a render misread it, and simulation outcomes reached him only as words.
- Decisions were long chat messages (correction, tables, options, recommendation) answered with
  one terse line. The same table was reissued ten times, and one approved limit later changed
  unasked.
- The agent worked alone for 15 to 90 minutes after each decision; new requirements typed
  meanwhile were held only in prose.
- Eight independent reviews found 66 defects that passing tests missed, while fighting stale
  outputs, a shared `build/` directory and edits made during review. Every reviewer rewrote its
  own measurement harness.
- Nothing computes stress, deflection or vibration.

The goal is a desktop application around this loop, mechanical engineering first. The owner
inspects parts, assemblies and simulations in 3D, comments, answers decision requests, explores
what-ifs and exports. A Claude Code agent keeps doing the engineering in code and drives the app.

## Decisions taken by the owner (2026-10-13)

- Generic tool in its own repository, `~/work/privat/workbench`, command `wb`, local git only.
  my-robot is the first project it opens.
- Personal tool first: this machine (Arch, Hyprland on Wayland, AMD GPU), uv, Claude Code. No
  installer, onboarding or other platforms now.
- The UI is for inspecting, measuring, commenting, deciding and live what-ifs. It never writes
  code; the agent applies changes.
- The app reaches the agent through herdr, where Claude Code already runs. No embedded terminal.
- After the first milestone: simulation replay, and engineering checks in parallel.
- Strength-of-materials analysis is wanted. Project Chrono and openCFS were assessed (below).

## Design

Principles:

1. The repo is the truth; the app is a lens and an inbox. Geometry, simulation and exports always
   come from project code in the project's uv environment.
2. Everything the agent can do is a `wb` command with JSON output. HTTP routes and MCP tools are
   generated from the same operation registry, so they cannot drift.
3. The owner and the agent look at the same thing: a saved view (target, camera, section, pose,
   highlights) that either side can create. The agent never moves the owner's camera.
4. Numbers gate, pictures are a glance: every view shows the measured facts it rests on.
5. No geometry plumbing of our own: tessellation, picking, measuring and animation stay upstream.

```
owner (Hyprland)
  kitty + herdr: claude (author) -- Bash `wb ...` / MCP shim
               \ claude (reviewer pane, cwd = snapshot)
  chromium --app (native Wayland) -- HTTP + WebSocket on 127.0.0.1 (token, Origin check)
                                         |
hub `wb serve` (own environment, no CAD imports)
  operation registry | review store | facts index | blob store | job observer
  generation supervisor | agent transport: hooks (always) + herdr (wake, state, panes)
        |                               |
kernel generation (project venv)   jobs (project venv)
  parameter overlay, then import robot.manifest      `wb run <target>` with own output dir,
  ocp-viewer-core host, anchors, fingerprints          log and status file
  MuJoCo models, mj_forward, recorder
```

| Piece | Choice | Reason |
|---|---|---|
| Viewer | `three-cad-viewer` 5.1 hosted through `ocp-viewer-core` 1.1, fed by `ocp-tessellate` 3.6 | Only stack that runs on build123d 0.13 and gives pickable faces and edges that map back to the exact shape in Python; section, explode, measure, tree and animation exist. Resolves against the project's pins. |
| Own overlay | Second three.js layer in the same GL context | Pins, arrows, contact and COM markers, later stress fields. The stock viewer has no per-vertex fields. |
| Window | `chromium --app` on native Wayland | Same engine as Electron, zero packaging. Launched with the stale NVIDIA variables unset. |
| Hub | Python (Starlette), installed with `uv tool install` | Keeps the web stack out of the robot's lockfile. |
| Kernel library | `workbench-kernel`, a dev dependency of the project | The only thing the project environment gains. |
| UI | Vite, React, TypeScript | Panels around the stock viewer: inbox, item, views, jobs, later sim and parameters. |
| Agent link | Claude Code hooks plus the herdr CLI behind one interface | Hooks pull on every prompt and tool call; `herdr agent prompt` wakes an idle agent; reviewer agents start in herdr panes. |

Mechanisms:

- **Generation.** A (code state, parameter overlay) pair in its own kernel process. Viewing,
  what-if, comparing HEAD with the working tree and a reviewer snapshot are all "open a
  generation". A new one builds while the old one keeps answering clicks. Rebuild happens when
  the agent's turn ends, on `wb show`, or on refresh; in between the last good scene shows a
  stale badge.
- **What-if.** The kernel replaces objects on the parameter module before anything else imports
  it. Verified on this repo: `robot_assembly` and `robot_spec` see the variant, footprint 329 mm
  equals the tipping study's "radius 175" row, and the variant builds in 3.2 s. A late patch is
  refused. "Apply" becomes a change request for the agent.
- **Review items.** One type with kinds: comment, finding, decision, measurement, change. Each is
  an append-only event file `review/R-0042.yaml` written only by the hub; state is whose move it
  is. A decision carries corrections first, frozen evidence tables, questions with options that
  can be answered partially, and the values the answer approves. A check fails when an approved
  value later differs. `review/INDEX.md` is generated and replaces the hand-kept open items.
- **Targets and anchors.** An item points at a part, parameter, fact, table row, recording time or
  file. Sub-shapes come later, in this order: a feature the project declares (its hole families
  already exist as functions), then a geometric signature in the part's own frame with a unique
  match required, otherwise "lost" with a ghost pin. Viewer indices are never stored.
- **Build ids.** Commit plus dirty hash, restorable through a snapshot ref written from a
  temporary index (branch, index and working tree untouched). Every output and item is stamped.
- **Agent messages.** Text typed into the agent is a fixed pointer ("Owner answered R-0042. Run
  wb inbox."). Content is always pulled as data with its author, which also confirms delivery.
  A blocked agent is never typed into.
- **Jobs.** Tests, studies and exports are detached processes with their own output directory
  and a status file. A 19-minute study survives a hub restart and its result must be acknowledged.
- **Simulation.** MuJoCo geoms as simulated, grouped by body, in the same viewer; CAD only as an
  optional ghost. Sliders call `mj_forward` in the kernel. Recordings are made on replay by
  wrapping `mujoco.mj_step`; each study row carries its recipe and shows the last surviving and
  first failing run.
- **Analyses.** Project Python that returns check objects (value, limit, unit, utilisation,
  source, credibility). Three tiers: calculation sheets, a beam and frame model, part FEA.

Project contract in my-robot (sketch against the real functions):

```python
# src/robot/manifest.py, named in [tool.workbench] of pyproject.toml; importing it builds nothing
wb = Project("my-robot", units="mm",
             params={"robot.params": ["BASE", "LIFT", "PLATFORM", "FITS", "RULES", "TROLLEY"]})

@wb.part("lower_plate", process="sheet", material="aluminium 3 mm", features={
    "mount_bolts": wb.holes("z", lambda: base_plates.mount_bolt_holes(P.BASE, _base()))})
def lower_plate(): return base_plates.make_lower(P.BASE, _base(), _lift(), P.FITS)

@wb.assembly("robot")
def robot(height=None): return assembly.robot_assembly(height)

@wb.scene("robot", cad="robot")
def robot_scene(payload=0.15): return mjcf.robot_spec(payload=payload)

wb.study("tipping", "scripts/tipping_study.py", table="tipping_study.json", minutes=15)
wb.check("tests", ["pytest", "-q"], minutes=7)
```

## Mechanical analysis: what was found

- Stack: plain Python with pint and sympy for calculation sheets; PyNite and sectionproperties
  for the frame tier (pure wheels); gmsh plus CalculiX for part FEA, with `ccx` as a pinned
  external binary run as a subprocess; scikit-fem as an independent cross-check. All wheels
  resolve against the project's pins.
- Project Chrono: left out. No wheels (`pip install pychrono` is an unrelated package), a
  separate 535 MB conda environment, no MJCF import, modal analysis not exposed in Python, sensor
  module needs NVIDIA. Lumped hinges in MuJoCo answer the column-sway question.
- openCFS: left out. It can do static and modal analysis of solids, but has no contact, beam or
  bolt features. Worth another look only for acoustics or actuators.
- For this robot stiffness and sway matter, not strength. First-order and unreviewed: column
  modes of the order of 7 and 14 Hz with an ideal clamp, lower with real joints.

## Findings in my-robot to fix first

| Finding | Where | Consequence |
|---|---|---|
| `reference_poses(model)` is called without the variant's front limit | `scripts/tipping_study.py:50`, default at `src/robot/stability.py:145` | The "front limit 75" comparison row picks its driving pose under the 35 mm rule. Current design unaffected. Effect not measured yet: report as a correction after review. |
| `robot_spec()` builds each base plate twice | `src/robot/mjcf.py:219,300,322` | About 5 of its 6.4 s. |
| Plate holes are cut one at a time | `src/robot/parts/base_plates.py:133` | 1.3 s per plate; against the repo's own batching rule. |
| A robot model is compiled at import | `tests/test_trolley.py:15` | Slows every test collection. |
| Scripts hardcode `build/` | five scripts | Author, reviewer and what-if runs overwrite each other. |
| Catalogue values differ | `src/robot/vendor/mgn12.py`, `extrusion.py` | Block mass 0.050 kg (repo 0.054); extrusion masses 5 to 10 % low. Unreviewed. |

## Spikes (throwaway code in `workbench/spikes/`, each ending in a short result note)

| # | Question | Pass | Box | If it fails |
|---|---|---|---|---|
| S1 | Can our page host the stock viewer and still reach scene, camera and picks, with an overlay? | 20 picks resolve to the right face in the kernel; pin stays glued while orbiting; 60 fps on the full robot | 2 d | Compose from lower-level exports; last resort an iframe with a 2D overlay |
| S2 | Do hooks and herdr deliver as documented? | Mid-turn context arrives after the next tool call; stop hook blocks once; idle agent is woken; nothing sent to a blocked agent | 1.5 d | Prompt-time pull plus herdr wake only |
| S5 | Can MuJoCo scenes play in the same viewer? | Robot scene loads under 5 s; 10 s replay smooth; poses match `data.xpos` within 1e-6 m | 2 d | Small own canvas for MuJoCo geoms |
| S3 | Are generations robust (swap, crash, late overlay)? | Old generation answers during a build; late patch refused | 1 d | One generation with a busy state |
| S4 | Rebuild to pixels | Warm restart shows the robot within 3 s after build; a lift change tessellates nothing | 1.5 d | Full re-show with progress |
| S7 | Is a reviewer snapshot isolated? | Imports resolve inside the snapshot; an edit in the main tree leaves its facts unchanged | 1 d | Copy with an explicit path and the same assertion |
| S6 | Do sub-shape anchors rebind safely? | Zero wrong bindings over real parameter changes | 1 d | Keep part, feature and frozen evidence only |
| S9 | Does one structural path match a closed form? | Plate or cantilever within 5 % | 2 d | Next solver on the list |

## Phases

Each phase is usable on my-robot by itself.

| Phase | Deliverable | Exit check | Left out |
|---|---|---|---|
| 0. See now, hygiene, spikes | Interactive viewer today (`ocp-viewer` plus `scripts/view.py`); fixes from the table above; workbench repo skeleton; spikes S1, S2, S5 | Viewer shows the robot on 127.0.0.1:3939; with `WB_OUT` set an export leaves `build/` untouched; one plate build per `robot_spec`; tests pass | All app code |
| 1. Hub, scene, inbox | `wb serve`, `wb open`; assemblies with tree, measure, section; saved views; comments and decision requests on part, parameter, fact, file; delivery through hooks and herdr; build ids and snapshots; MCP shim generated last | Scripted round trip: agent asks, owner answers in the page, agent is woken and fetches the answer. A pick returns `lower_plate`. Killing the kernel keeps the last scene with a crash banner | Sub-shape anchors, sim, exports, what-if |
| 2. Simulate and replay | Scenes, joint and lift sliders, clearance, COM and support overlays, recordings, threshold pairs from study rows, comments at a recording time | A trip just above the limit ends over 20 degrees of tilt and just below stays under; poses match the simulation | CAD ghost, driving |
| E. Engineering checks (parallel, no app needed) | See next section | Each check asserted in pytest and independently reviewed | |
| 3. Make | Scripts as jobs with declared outputs; output panel with previews and staleness by fingerprint; ledger of assumed versus measured values driving holds; as-made stamps; generated parts list | Exporting `lower_plate.dxf` fails naming the unmeasured wheel width, and succeeds after a recorded measurement | Slicer gate, drawings, nesting |
| 4. Review depth | Feature and sub-shape anchors; facts diff and part-level geometry diff against HEAD; approved-value drift check; reviewer on a snapshot with findings as items; trust levels | Anchor suite with zero wrong bindings; an edit during a review leaves the snapshot's facts unchanged | Boolean geometry diff |
| 5. What-if | Overlay generations, parameter panel with ledger badges, variant facts diff, "apply" as a change request | Radius 175 reports footprint 329; an overlay run writes nothing under `build/` or `facts/` | Optimisation, sweeps in the UI |
| 6. Analyse in the app | Field layer in the overlay (stress, deflection, mode shapes), check tables with utilisation, study plots | One benchmark field shown and matching its closed form | |

Later, not planned in detail: electronics and control modules, a chat panel, packaging.

## Engineering track (project and kernel library, in order of value)

1. Check objects and a single `wb check` entry point that fails on zero assertions; canonical
   tracked facts so `git diff facts/` is the "what changed" view; a shared measurement library
   (interference, clearance, mass and COM, holes, envelope, CAD against simulation) whose every
   result states method, tolerance and counts.
2. Wheel servo shaft load. No rating is published for the STS3215; a comparable servo's rating
   suggests the load is too high. Outcome: a decision request on an outboard bearing.
3. Column, foot and carriage stiffness: section properties of the real profile, a frame model,
   then hinges with that stiffness and interface force sensors in `src/robot/mjcf.py`, and the
   stability studies rerun with sway.
4. Bolt sheets: M3x65 through the printed mounts, M5 into the column, M3 into the rail blocks.
5. Printed carriage by FEA (after S9): stress across layers, deflection at the arm plate.
6. Housing thermal balance as a lumped model.
7. Later: wheel mounts, base plates as defeatured shells, omni rollers and the doorway
   threshold, reach and sensor coverage, tolerance sampling, fasteners and joints as objects.

## Repository layout

```
~/work/privat/workbench/            uv workspace, own CLAUDE.md carrying my-robot's working rules
  packages/hub/src/workbench/       cli.py, ops/ (registry), hub/, review/, transport/, mcp/
  packages/kernel/src/workbench_kernel/
                                    api.py (manifest), overlay.py, host.py, anchors.py,
                                    fingerprint.py, sim.py, runner.py, paths.py, facts.py,
                                    measure/, ledger.py, snapshot.py
  ui/                               scene/ (host page, overlay), inbox/, views/, jobs/, sim/
  plugin/                           hooks, skill, reviewer agent definition
  examples/minimal/                 3 parts, 1 assembly, 1 scene, 1 study, 1 assumed parameter
  tests/  e2e/  spikes/
```

## Changes in my-robot

| File | Change | Phase |
|---|---|---|
| `pyproject.toml` | dev dependency `ocp-viewer`, later `workbench-kernel`; `[tool.workbench]` | 0, 1 |
| `scripts/view.py` (new) | show the assemblies in the standalone viewer | 0 |
| `src/robot/paths.py` (new) and the five scripts | `build_dir()` honouring `WB_OUT` | 0 |
| `src/robot/mjcf.py` | build each plate once in `robot_spec` | 0 |
| `tests/test_trolley.py` | module-level compile becomes a fixture | 0 |
| `scripts/tipping_study.py`, `src/robot/assembly.py` | pass the variant's front limit; trolley passthrough | 0, reviewed |
| `src/robot/parts/base_plates.py` | batch the hole cuts; assert same fingerprint and hole counts | 0 |
| `src/robot/manifest.py` (new), `review/` (new), `.gitignore`, `.claude/settings.json` | project contract, hooks | 1 |
| `CLAUDE.md` | open items point to `review/INDEX.md`; `wb` commands | 1 |
| `src/robot/stability.py`, `scripts/tipping_study.py` | call log on the simulation functions; rows carry their replay recipe | 2 |
| `ledger/`, `facts/`, `evidence/` (new), `PARTS.md` generated | provenance, tracked facts, owner-supplied sources | 3 |

Reused as they are: `src/robot/assembly.py` (labelled children), `src/robot/mjcf.py` (`robot_spec`,
`stand_spec`), `src/robot/stability.py` (poses, clearance pairs, swings), `src/robot/render.py`
(headless contact sheet stays the agent's own picture), the export scripts as jobs.

## First steps after approval

1. my-robot: add `ocp-viewer` and `scripts/view.py`, confirm the robot shows in a Chromium
   window. Sonnet subagent.
2. my-robot hygiene from the findings table, with targeted tests and one full suite run. Sonnet
   subagent, then a fresh reviewer.
3. my-robot: the front-limit correction, measured before and after, reviewed independently and
   reported as a correction. Opus subagent.
4. Create the workbench repository with its skeleton and `examples/minimal`. Sonnet subagent.
5. Spikes S1 and S2 in parallel, then S5. Opus subagents; each verdict re-measured by a fresh
   reviewer.
6. Report the verdicts and any change to the stack before Phase 1 starts.

At most three subagents run at the same time, reviewers included (owner's limit, to stay within
the usage budget). Steps 1 to 4 run one or two at a time; the spikes never more than two in
parallel, leaving room for a reviewer.

Commits are made per step in my-robot and the new repository; nothing is pushed.

## Verification

- Kernel and hub: pytest on `examples/minimal` (review fold, overlay refusal, job envelope, build
  ids, fingerprints), plus an opt-in contract test that loads my-robot's manifest, opens the
  scene and computes facts.
- UI: Playwright against the served page with assertions on API and DOM state, not pixels.
- Agent round trip at three levels: a scripted fake agent using `wb` and recorded hook input on
  every run; `claude -p` with the plugin on the example project on demand; one live herdr smoke.
- Silent-failure tests: hub down means hooks do nothing quickly; an empty measurement selection
  is an error; a check with zero assertions fails; an overlay result is never taken as baseline.
- Budgets (rebuild latency, hook overhead, zero wrong anchor bindings) live in one file with
  tests, as `DesignRules` does for the robot.
- Per phase, the exit check in the table is run on my-robot and shown to the owner.

## Risks and what will not be built

- Viewer stack has one maintainer and released four packages today: pin exact versions, one
  adapter module per side, a golden-scene test on upgrade. Licences allow vendoring.
- Silent wrong state (stale generation, live code in a snapshot, overlay leaking into baseline):
  build id and overlay watermark on every output, code-root assertion in the kernel.
- Wrong anchor: unique match or lost, never a guess.
- The local API starts jobs: loopback only, token, Origin check, manifest targets only.
- herdr dependence: behind the transport interface; hooks alone lose only the idle wake.

Not built: an Electron shell or embedded terminal, an own tessellator or viewer protocol,
browser-side physics, a code editor, UI controls that write parameters, a Jupyter kernel, a
database, persistent topological naming, auto-commits on the working branch, Project Chrono or
openCFS integration, an installer.

## Owner actions, optional

- `~/.zprofile` still exports `XWAYLAND_NO_GLAMOR=1`, `LIBVA_DRIVER_NAME=nvidia` and
  `__GLX_VENDOR_LIBRARY_NAME=nvidia` although the only GPU is AMD. GLX fails and XWayland
  renders in software. The app window unsets them for itself; removing them fixes other programs.
- The open measurements in `CLAUDE.md` (wheel, power bank and boards, trolley, IMU) stay as they
  are; from Phase 1 they appear as measurement requests.
