# S5 result: MuJoCo scenes in the stock CAD viewer

Spike S5 of `docs/plan.md`, run 2026-10-14. (M) means measured in this spike, (R) reasoned.

Setup: my-robot frozen at `c44f5b2` in the scratchpad (`robot.__file__` checked to point into the copy,
`vendor_cad` symlinked read-only), `robot_spec(payload=0.15).compile()`; MuJoCo 3.15.0; ocp-viewer 1.2.0
with ocp-viewer-core 1.1.2 and ocp-tessellate 3.6.2, three-cad-viewer 5.1.3 as bundled; the stock server
`python -m ocp_viewer --port 3959` and its stock `/viewer` page, unmodified; headless Chromium 153 driven
over CDP (Node 22 builtins, no packages). The server ran with a private `HOME`, so the owner's
`~/.ocpvscode` was not touched (checked before and after).

## Verdict

| S5 criterion | Result | Numbers |
|---|---|---|
| Robot scene loads under 5 s | **PASS** (M) | Visual scene (74 geoms, 816k triangles, 10.7 MB): 0.33 to 0.71 s on the AMD GPU, 2.2 to 2.5 s on SwiftShader. Collision scene: 0.17 to 0.33 s, and 0.75 s on SwiftShader. |
| 10 s replay smooth | **PASS on the AMD GPU** (M); FAIL on software GL (M) | GPU: 22 playbacks of 10 s, 13,195 frames, median 16.7 ms, p95 at most 16.8 ms, clock 1.000. Five frames were over 25 ms (four at 33 ms, one at exactly 50.0 ms), none over 50 ms. SwiftShader: 3.5 fps for the visual scene, 23 fps for collision. |
| Poses match `data.xpos` within 1e-6 m | **PASS** (M) | Max error 6.1e-8 m over 4 replays × 20 sampled times × 21 bodies, identical on all three GL paths. Every drawn geom against MuJoCo's own `geom_xpos`: max 6.1e-8 m. |

"Smooth" was fixed in the driver before measuring: median frame ≤ 17.5 ms, p95 ≤ 20 ms, max ≤ 50 ms,
and an animation clock within 2 % of real time. The one 50.0 ms frame sits exactly on that limit.

The stock viewer can show and replay the simulation, so the fallback canvas is not needed. Three
things Phase 2 needs are out of reach of the stock *messages*, though: moving bodies live, replacing a
loaded recording, and starting playback. Each takes a few lines of page code, which S1's host page
provides (measured below).

## Corrections and additions to the brief

1. **Track offsets are relative to the group's pose when the track is added**, not to the scene's
   base pose. The brief's "offsets from the object's base pose" is true only if nothing moved the
   group before. If anything moved it first (a direct transform, explode, an earlier animation that
   was applied), the replay is shifted without any error. Measured: one direct slider update
   followed by an animation update left the arm 0.37 m off.
2. **`comms.send_data` skips the config rename.** `Session.send_data` renames the config to the
   page's camelCase names and adds the version handshake. Sending through `comms.send_data`
   directly, the page silently ignored `reset_camera` and kept the splash camera at zoom 43.5. All
   sends here go through `viewer.config.session.send_data`.
3. **The stock page exposes no handle to its Viewer**, which stays inside `createPage`. Two ways in
   both worked without changing the page (M). An injected script imports the same module URL (the
   same module instance) and wraps `Viewer.prototype.render`. The import can resolve after the
   onload splash render; it did in 3 of the 6 runs that recorded this (and made one earlier run fail
   before the fallback existed), and calling the page's own
   global `showViewer()` re-renders the splash. CDP `Runtime.queryObjects(Viewer.prototype)` found
   exactly one instance, the same object.
4. Model counts: 42 mesh geoms use 36 mesh assets, of which **18 are unique**. The left and right
   arm copies are byte-identical. The model has 347,128 mesh vertices in total, and the visual
   scene draws 816k triangles (M).

## Q1 Scene conversion (M)

`py/sim_scene.py` converts a compiled model:
- **Groups:** one group per body, flat under the scene root. Each group sits at the body's world pose
  (mm, quaternion x, y, z, w), so a group's transform compares directly with `data.xpos`.
- **Leaves:** one leaf per geom, placed by `geom_pos` / `geom_quat`.
- **Meshes:** raw leaves from `mesh_vert` / `mesh_face` / `mesh_normal`. In this model the normals
  are indexed like the vertices, so they are used as they are. Meshes carry no B-rep and no edges.
- **Shared geometry:** sent once in `instances` and referenced by `{"ref": n}`, as ocp-tessellate
  does.
- **Selection:** `which="visual"` (geom groups 0 to 2, MuJoCo's default), `"collision"` (contype or
  conaffinity set), `"all"`, or an explicit `groups=[...]`.
- **Primitives, two ways:**
  - `"direct"`: numpy tessellation, with box faces and edges as a CAD box would have.
  - `"brep"`: build123d solids tessellated by ocp-tessellate. A capsule is a cylinder fused with two
    spheres.
- **Floor:** the plane is drawn as a 1.2 m square. At its 5 m size it frames the camera on the floor.

| variant | build (s) | wire (MB) | leaves | instances | unique triangles | drawn triangles |
|---|---|---|---|---|---|---|
| visual, direct | 0.08 to 0.10 | 10.70 | 74 | 43 | 331k | 816k |
| visual, brep | 0.22 (0.08 cached) | 10.89 | 74 | 43 | 337k | 830k |
| collision, direct | 0.03 | 0.64 | 76 | 36 | 17k | 59k |
| collision, brep | 0.14 (0.04 cached) | 0.86 | 76 | 36 | 23k | 102k |

Every leaf's world pose, composed from the tree, was checked against MuJoCo's `geom_xpos` /
`geom_xmat`: max error 1.1e-16 m. Without shared instances the visual message would be about 2.5 times
larger (R: 816k drawn versus 331k unique triangles). For context, a cold kernel spends 1.6 s on
`import robot` and 0.86 s on `robot_spec` plus compile (M); neither is counted in the load times.

## Q2 Recording (M)

`py/recorder.py`: `with recording(model, rate_hz=60) as rec:` replaces the module attribute
`mujoco.mj_step` for the block. One run is one `MjData` until its time goes backwards. Each run is
sampled at step boundaries on a 60 Hz grid of simulated time (intervals 0.015 and 0.020 s, mean 1/60
s), plus the final state when the block exits.

- **Time stamps.** After `mj_step`, `data.xpos` belongs to the state before the step, while
  `data.time` and `data.qpos` have already advanced. Samples are therefore stamped with the time
  before the step and store the `qpos` from before the step. Forward kinematics of every stored
  `qpos` reproduces the stored `xpos` / `xquat` exactly (0.0 error, all samples). A recorder that
  pairs `data.time` with `data.xpos` after the step is off by one step: up to 8.3 mm during the fall
  (1.65 m/s × 5 ms).
- **Final state.** Without the extra final sample, the fall's recording ended at 39.5° instead of
  the 40° at which `simulate` stopped.
- **No disturbance.** The swing's peak tilt is bit-identical with and without the recorder
  (0.030919001962465345°). The trips fall or stand the same with and without it. Recording cost the
  swing 0.230 s against 0.222 s plain.
- **Trip limit.** The study's own `limit()` bisection gives 0.531 m/s for "stretched" at the lift top,
  direction 0 (bracket 0.0078). That agrees with the 0.53 m/s in the existing
  `build/tipping_study.json` at payload 0.15. `reference_poses` took 3.85 s and is cached in
  `results/poses_payload0.15.json`.

| run (recipe in `results/q2_recording.json`) | samples | simulated time | raw float64 | npz | as track message |
|---|---|---|---|---|---|
| swing up to stretched, lift 0.6 m, 1.0 rad/s | 364 | 6.04 s (1 s settle + swing) | 489 KB | 259 KB | 1.33 MB JSON |
| trip at 0.539 m/s (just above the limit), falls | 195 | 3.22 s, ends at 40.0° | 262 KB | 161 KB | 0.71 MB JSON |
| trip at 0.531 m/s (the limit), stands | 242 | 4.00 s, peak 9.85° | 325 KB | 202 KB | - |

Raw size per sample: 21 bodies × (xpos + xquat) plus qpos, about 1.3 KB.

## Q3 Playback (M)

`py/tracks.py` turns world poses into per-body offsets against the scene's base pose:
`p - base_p` and `conj(base_q) * q`. That gives 40 tracks (20 moving bodies × position and
quaternion), sent as the stock `animation` message. The same tracks sent through ocp_viewer's own
`Animation` helper replay identically. The helper accepts only paths of the last `show`, so
`viewer.last_paths` has to be set first. Playback was started with the stock UI's Play button,
clicked through CDP. The load time runs from the start of conversion in the kernel process to the
first frame after the page has drawn the scene.

| GL path (headless) | window | visual load (ms) | collision load (ms) | visual swing | collision swing | visual trip |
|---|---|---|---|---|---|---|
| SwiftShader | 1600×1000 | 2216 to 2534 | 749 to 753 | 3.6 fps, median 267 ms | 22.8 fps, median 50 ms | 3.5 fps |
| AMD, ANGLE Vulkan | 1600×1000 | 334 to 479 | 170 to 220 | 59.8 fps, max 50.0 ms | 60.0 fps, max 16.8 ms | 60.0 fps |
| AMD, ANGLE GL-EGL | 1600×1000 | 421 to 713 | 197 to 324 | 60.0 fps, max 16.8 ms | 60.0 fps | 60.0 fps |
| AMD, ANGLE Vulkan | 2560×1440 | 459 to 677 | 254 to 328 | 59.9 fps, max 33.3 ms | 60.0 fps | 60.0 fps |
| AMD, ANGLE GL-EGL | 2560×1440 | 405 to 491 | 220 to 226 | 60.0 fps | 60.0 fps | 60.0 fps |

- **Repeat runs.** Five more 10 s visual playbacks per hardware path: Vulkan 2,999 frames with one
  over 20 ms (33 ms); GL-EGL 2,998 frames with two (33 ms). All 10 runs meet the criteria. S1 was
  using the same GPU at the same time, which may account for the rare missed vsync (R).
- **Where the time goes.** The page side of a visual load is 200 to 370 ms (decode, scene and tree);
  the GPU's first frame takes 2 to 7 ms. Per-frame CPU cost while playing is 1.7 to 3.2 ms on the
  GPU paths (1.1 to 1.3 ms in an earlier run).
- **What worked.** Both hardware flag sets that S1's probe found, `--use-angle=vulkan
  --enable-features=Vulkan --ignore-gpu-blocklist` and `--use-gl=angle --use-angle=gl-egl
  --ignore-gpu-blocklist`, ran everything headless with the stale NVIDIA variables and
  DISPLAY/WAYLAND_DISPLAY unset. The windowed `chromium --app` on Wayland was not measured.

## Q4 Pose accuracy (M)

For each recording, the page was set to 20 evenly spaced keyframes through the public
`viewer.setRelativeTime`, using the clip's own float32 times. At each one it read every body group's
`matrixWorld` and every leaf's.

| replay | body poses | max position error | max angle error | leaf poses against `geom_xpos` from FK of the recorded qpos |
|---|---|---|---|---|
| swing | 420 | 6.05e-8 m | 9.9e-8 rad | 1,480, max 6.13e-8 m |
| trip above the limit | 420 | 5.75e-8 m | 1.2e-7 rad | 1,480, max 5.75e-8 m |
| trip at the limit | 420 | 3.05e-8 m | 1.2e-7 rad | 1,480, max 3.16e-8 m |
| swing through the stock `Animation` helper | 420 | 6.05e-8 m | 9.9e-8 rad | 1,480, max 6.13e-8 m |

- The root group's world matrix is the identity, so the viewer's world is MuJoCo's world in mm.
- The residual is float32 rounding in three.js keyframe storage. The worst case is half a float32
  step of the value in mm (R): 6e-8 m up to 1 m from the origin, 4.9e-7 m at 8 to 16 m, 9.8e-7 m at
  16 to 33 m. That is fine for the stability scenes; a long drive is the limit.

## Q5 Picking (M)

- **Method.** Every leaf whose centre pixel is visible was a candidate (31), with visibility decided
  by an independent three.js raycast in the page. Each candidate got a double-click with no tool
  active, as S1 does for part picks.
- **What the page reports.** It sends `lastPick {path, name}` in its status frame, and Python reads
  the same value through the server's `status()`.
- **Results.** 31 of 31 picks map back through the scene's path table to the right geom id, geom
  name, body id and body name, consistent with the model. 28 of 31 equal the raycast's first hit.
  The three misses returned an adjacent part on thin or small targets: the rail, 6 px wide on screen,
  gave the camera mast; a 22 px servo gave the motor holder; a 19 px drive box gave `deck_solid`.
  Mesh and primitive leaves pick alike.
- **Path scheme.** A pick path is `/sim/<body>/<leaf>`. The leaf is the geom name, or
  `g<id>_<mesh name>` / `g<id>_<type>` for the 62 unnamed geoms. The geom id is therefore also
  readable from the name itself.

## Q6 Slider round trip (M)

Each update sets one joint (`left_shoulder_lift`, ±0.6 rad, alternating), runs `mj_forward`, sends,
then reads all 21 body poses back in the page. Latency runs from the joint change in Python to the
first animation frame after the page has drawn and the GPU has finished. Each mechanism starts on a
freshly shown scene. Values below are medians on the AMD GPU (Vulkan and GL-EGL runs); SwiftShader
in brackets.

| mechanism | needs | shown after | pose error |
|---|---|---|---|
| `s5_body_poses` message through the stock server, a new connection per message (as ocp_viewer's comms does), page sets the group transforms | about 20 lines of page code | 8 to 11 ms, p95 13 to 16 ms (420 ms) | 0 (same float64 values) |
| the same over one held websocket | the same | 6.6 to 8.9 ms (420 ms) | 0 |
| stock: re-show the whole scene with new body locs | nothing | 290 to 350 ms, 10.7 MB per update (4.3 s) | 0 |
| stock: one-pose `animation` message + `set_relative_time` | nothing | 19 to 27 ms (810 ms) | 1.5e-8 m on the first update, then 0.37, 0.13, 0.51 m |

Breakdown of the direct path: `mj_forward` 0.15 ms, message build 0.1 ms, connect and send 1.8 to
2.4 ms (0.1 ms when held), the page has the message 1.2 to 3.2 ms after the joint change, then 2.5 to
6 ms to apply and submit the render.

The stock animation path breaks for a reason that was measured directly. Each animation message adds
its tracks to those already loaded: the loaded count grew 42, 84, 126, 168, and no stock message
clears them. A second recording loaded the stock way replays 0.48 m off. With the page calling the
viewer's own `controlAnimation('stop')` and `clearAnimation()` first, it replays at 5.7e-8 m without
re-sending the 10.7 MB scene.

## What the viewer could do (M)

- Show a hand-built tree grouped by body: raw mesh leaves, shared instances, B-rep or direct
  primitives, visual or collision sets. The tree panel lists `sim / <body> / <geom>`.
- Replay 20 bodies × 364 keyframes at 60 fps with the stock Play, Pause, Stop and time slider, at
  real-time speed, 2560×1440 included, exact to float32.
- Scrub to any time from Python with the stock `set_relative_time` command.
- Report picks as `lastPick {path, name}` to Python through the stock server.
- Relay custom message types unchanged. The stock server forwards any `D:` payload, and the stock
  page ignores types it does not know, so page code can add messages without touching the server.

## What the viewer could not do

- Hand out its Viewer object on the stock page (M). It needs an injected script, CDP, or a page of
  our own.
- Move a body, clear an animation, or start playback through any stock message (M). The only stock
  routes are a full re-show, at 0.3 s and 10.7 MB per update, or the UI button.
- Load a second recording through stock messages: replay is 0.48 m off (M). Tracks are also based on
  the group's current pose, so any other mover corrupts a replay with no error (M).
- Accept a config sent through `comms.send_data`: the snake_case keys are silently ignored (M).
- Let ocp_viewer's `Animation` helper address a scene not sent by `show`, unless `viewer.last_paths`
  is set (M).
- Pick thin or small parts reliably: 3 of 31 returned an adjacent part (M).
- Play smoothly on software GL: 3.5 fps with the visual meshes (M).
- Not tested: the distance and properties tools on raw mesh leaves, and the Python measurement
  backend. No backend mapping is sent for the sim scene.

## Consequences for Phase 2 (R)

- **Page.** Host the viewer in our own page (S1) and add three small functions:
  - set body poses;
  - load a recording: stop and clear the loaded clip, restore the base pose, then add tracks;
  - play, pause and seek.

  Send absolute world poses, and compute offsets in the page against each group's pose at load time,
  so that no earlier mover can shift a replay.
- **Kernel.** Keep this spike's three pieces as they are:
  - the scene adapter, which sends through `Session.send_data` or our own transport with the
    camelCase config;
  - the recorder, with pre-step time stamps and a final sample;
  - the forward-kinematics consistency check, as a test.
- **Data budget.** Recordings are about 1.3 KB per sample raw and 220 KB of JSON per simulated second
  as tracks. Use binary buffers or decimation if studies grow to minutes.

## Fallback

Not needed: all three criteria pass on the target GPU. If an own canvas were ever needed, the
conversion (`sim_scene.py`) and the recorder carry over unchanged; only `tracks.py` and the page
would change (R).

## Files

- `py/sim_scene.py`: conversion (Q1).
- `py/recorder.py`: the `mj_step` recorder (Q2).
- `py/tracks.py`: tracks and the animation message (Q3).
- `py/s5host.py`: kernel-side host with JSON lines on stdin and stdout. It also holds the slider
  mechanisms, the pose comparisons and the forward-kinematics checks.
- `py/q1_convert.py`, `py/q2_record.py`: standalone runs for Q1 and Q2.
- `py/summarize.py`: collects every result into `results/summary.json`.
- `driver/harness.js`: the measurement script injected into the stock page.
- `driver/lib.mjs`: process groups and the CDP client.
- `driver/run_s5.mjs`: the driver, with sections `load`, `play`, `poses`, `pick`, `slider`, `repeat`
  and `clear`.
- `results/`:
  - `q1_conversion.json`, `q2_recording.json`, `recording_*.npz`;
  - one `s5_<gl>[_<window>].json` per run;
  - driver logs, and screenshots for a glance (`load_*`, `play_*`, `poses_*_mid/_end_*`, `pick_*`,
    `slider_*`);
  - `summary.json`.

Rerun (a fresh frozen copy as in the brief; paths overridable with `S5_SNAPSHOT` and `S5_SCRATCH`):

```
cd <snapshot> && env -u VIRTUAL_ENV uv run python <spike>/py/q1_convert.py
cd <snapshot> && env -u VIRTUAL_ENV uv run python <spike>/py/q2_record.py
cd <spike> && node driver/run_s5.mjs --gl=swiftshader,vulkan,egl          # about 6 min
cd <spike> && node driver/run_s5.mjs --gl=vulkan,egl --sections=load,play --size=2560x1440
cd <spike> && node driver/run_s5.mjs --gl=vulkan,egl --sections=repeat --size=1600x1001
cd <spike> && node driver/run_s5.mjs --gl=vulkan --sections=slider --size=1600x1002
cd <spike> && node driver/run_s5.mjs --gl=vulkan --sections=clear --size=1600x1003
python3 py/summarize.py
```

The driver starts and always stops the ocp_viewer server (port 3959), the Python host and Chromium
(CDP port 9359), each as its own process group. After every run, no spike process remained.
`s5_swiftshader.json`, `s5_vulkan.json` and `s5_egl.json` were written before the viewer-access check
existed. That check is recorded in the `*_1600x1001` and `*_2560x1440` runs.

Not yet independently reviewed: per the plan, a fresh reviewer re-measures this verdict.

## Independent review (2026-10-14)

A fresh reviewer re-ran claims 1 to 3 on its own port with the spike's unmodified harness, on a
frozen my-robot copy, with nothing else using the GPU. All five claims confirmed; none refuted.
Measured by the reviewer: visual scene loads in 268 to 315 ms (815,806 triangles, equal to an
independent count in the page); a 10 s replay has median and p95 frame interval 16.70 and
16.80 ms with no frame over 20 ms and the animation clock at 0.99996 of real time; replayed poses
are within 6.05e-8 m over 420 comparisons; peak tilt is bit-identical with and without the
recorder; stock tracks accumulate (80 after two loads) and replay 0.478 m off, 5.75e-8 m when the
page stops and clears first.

Corrections to this note:

- The 8.3 mm lag of a naive after-step stamp was computed as peak body speed times the time step,
  not measured, although it sits under a "measured" heading. The reviewer measured it at the full
  physics rate: 8.2567 mm, equal to the computed value for the fall; for the gentler swing,
  3.0 mm measured against 2.85 mm computed.
- The loaded-track counts "42, 84, 126, 168" in the corrections section are not backed by any
  saved result; the saved runs and the reviewer's reproduction show 40 tracks per load.
