# S1: our page hosting the stock CAD viewer

Question (plan, spike S1): can our own page host the stock viewer (three-cad-viewer through
ocp-viewer-core's JavaScript half) and still reach its scene, camera and pick events, draw our own
overlay in the same canvas, and stay fast on the full robot?

Date 2026-10-14. Machine: this one (Arch, AMD Radeon AI PRO R9700 / Navi 48, RADV and radeonsi),
Chromium 153.0.8010.36, Node 22.23.2. Stack: three-cad-viewer 5.1.3 (bundles three r184),
ocp-viewer-core 1.1.0 (npm) / 1.1.2 (Python), ocp-tessellate 3.6.2, build123d 0.13.0.
Robot: frozen copy of my-robot at 936dffa (`robot.__file__` checked to point into the copy;
`vendor_cad` symlinked read-only). Everything was measured in headless Chromium; no window was
opened on the owner's screen.

**M** = measured by running, **R** = reasoned from code or documentation, not measured.

## Verdict per S1 criterion

| Criterion | Verdict | Evidence |
|---|---|---|
| 20 picks resolve to the right face in the kernel | **PASS** (M) | 20/20 on the GPU (ANGLE/Vulkan) and 20/20 on SwiftShader; each resolved face `IsSame` the intended one, the aimed point lies on it (max 7e-15 mm), its centre projects within 0.47 px of the click (limit 2 px) |
| Pin stays glued while orbiting, hidden by geometry | **PASS** (M) | head centroid 0.013 to 0.085 px from an independent projection in 3 orbit views (GPU and SwiftShader runs); in a view where exact ray casts put every pin and arrow sample behind the rear wheel and housing, 0 overlay pixels; control without depth test: head at 0.06 to 0.07 px |
| 60 fps on the full robot | **PASS, headless on the owner's GPU** (M); visible window not measured | 1580x861 and 2540x1301: vsync-paced orbit 60.0 fps, frame interval p95 16.8 ms, 0 of 290 frames over 25 ms, via ANGLE/Vulkan and ANGLE/GL-EGL. One frame costs 4.0 to 4.3 ms median, 5.5 to 5.8 ms p95, including a full wait for the GPU; GPU time 0.76 to 0.92 ms median (timer query). A visible Wayland window was not measured (rule: no windows on the owner's screen); one command for it: `./run_visible.sh` |

No fallback is needed. The plan's fallbacks (compose from lower-level exports; an iframe with a 2D
overlay) were not tried.

Not yet done: an independent re-measurement by a fresh reviewer (plan, step 5). Commands are under
"Rerun".

## Corrections to the numbers in the brief

- Tessellation of `robot_assembly()` takes **2.1 to 2.3 s** in a fresh process, not about 7 s (M,
  six cold runs: 2.07 to 2.27 s; build 4.2 to 4.5 s). One more cold run measured 3.20 s with the
  build at 7.3 s; the cause was not established (other agents were working on the machine). `scripts/view.py`'s "sent 58
  parts in 6.44 s" is build plus show. In a process that has tessellated once, ocp-tessellate's
  cache brings a re-show down to 0.07 to 0.40 s (M).
- The tessellation the viewer receives holds **214,342 triangles** for the 58 parts (163,481 in its
  55 unique instances; M, counted in the payload), not about 484k. The GPU holds 215,050, the rest
  being the viewer's own helpers (M). One ordinary frame draws 766,511 triangles in 182 to 198 draw calls (M,
  three's `renderer.info` summed over one `update()`), because the viewer draws front and back
  faces and its own helpers. Where the 484k came from (another tolerance) is not known (R).

## What was built

`spikes/S1-viewer-host/`:

- `index.html`, `src/main.ts`, `src/overlay.ts`, `src/ocp-viewer-core.d.ts`: the page. Vite 8.3.4 +
  TypeScript 7.0.2, exact pins in `package.json`, `pnpm-lock.yaml`. The npm files of
  three-cad-viewer 5.1.3 and ocp-viewer-core 1.1.0 are byte-identical to the copies `ocp_viewer`
  1.2.0 serves (M, sha256).
- `host/s1host.py`: the Python host. Our `Comms` over one websocket to our page, serving `dist/`, the
  core's `ViewerBackend` for measurements, pick resolution with exact checks, ray casting, incremental
  updates. Its own uv project (`pyproject.toml`, `uv.lock`): `robot` is an editable path dependency
  on the snapshot, and every other version is constrained to my-robot's lock at 936dffa.
- `driver/`: `run_s1.mjs` (Q1 to Q5), `gpu_probe.mjs`, `visible.mjs`, `smoke.mjs`, `summarize.mjs`,
  `lib.mjs` (process groups, CDP client). Node builtins only.
- `results/`: JSON per run, run logs, screenshots.

## Q1. Hosting: the stock viewer in our page, with our transport (M)

The page calls `createPage({Viewer: HostViewer, Display, Timer, send, overrides})` from
ocp-viewer-core and feeds it messages with `page.handleMessage(obj)`. Our websocket carries the
core's messages and our own (`wb_overlay`, `wb_set_locs`, `wb_update_parts`). The Python side is
ocp-viewer-core's own show pipeline (`Session`, `Config`, `Viewer`) over our `PageComms`.

Each check is a real CDP click on the stock DOM, checked against viewer state, host state and drawn
pixels (GPU run; SwiftShader in its section below):

| Stock feature | Result |
|---|---|
| Tree | 58 leaf groups. The tree is virtualised: only about 18 rows exist in the DOM |
| Toolbar | "top" button: view direction (0, 0, 1); "iso" back; the host's status has the same camera position |
| Tree visibility | eye icon of `upper_deck`: state [0,0], host received the same state, drawn pixels drop; second click restores [1,1] |
| Clipping | Clip tab turns local clipping on; slider plane 1 from 1000 to 107.1, host received `clip_slider_0` 107.1, drawn pixels 108,918 to 90,641; Tree tab turns it off |
| Measure, properties | face picked, Python backend answers, panel shows area 30924.501 = exact `BRepGProp` area |
| Measure, distance | two faces, panel shows 64.350 = exact `BRepExtrema` 64.3497 |
| Explode | button, then its slider: parts move 144 to 671 mm; button again: back to 0 mm error |
| `getImage` | PNG data URL, 100 kB |

Through `createPage`, ocp-viewer-core sets `externalMeasurementBackend: true`, so the measure tools
wait for the Python backend (M: the panels open only after our host's `ViewerBackend` answers). A
host that does not answer `selectedShapeIDs` gets panels that never open, with no error (R, code:
the panel waits on a poll that has no timeout).

## Q2. Picks to Python (M)

Clicks go through the stock path: the properties tool is active, CDP moves the pointer, the driver
waits for the viewer to render (the hover pick happens inside a render), then presses and releases.
The viewer notifies `selectedShapeIDs`. The page forwards it with the pointer position and camera
state. The host resolves the id through `ViewerBackend.model` and checks it independently of the id
string:

- `IsSame` with the intended face;
- distance from the aimed point to the resolved face (exact, `BRepExtrema`);
- the aimed point projected by Python's own camera maths (written separately, from position,
  quaternion, zoom and frustum as the page reports them) against the click position.

The intended faces come from exact geometry: area centroid on the face, facing the camera, at least
14 px from the face's boundary on screen, and visible (nearest hit of exact ray casts through the
pixel and a 5 px ring around it, against all 2,667 faces). Views are aimed at each part and zoomed to
its size.

GPU run (ANGLE/Vulkan), 5 parts x 4 faces:

| # | intended face | resolved | aimed at | view az/el, zoom | edge margin px | click vs Python projection px | aimed point to resolved face mm |
|---|---|---|---|---|---|---|---|
| 1 | camera/faces/faces_5 | same, IsSame | centroid | -15/80, 3.71 | 61 | 0.422 | 0.0e+0 |
| 2 | camera/faces/faces_2 | same, IsSame | centroid | -105/5, 3.71 | 60 | 0.237 | 0.0e+0 |
| 3 | camera/faces/faces_3 | same, IsSame | centroid | 75/5, 3.71 | 60 | 0.237 | 0.0e+0 |
| 4 | camera/faces/faces_1 | same, IsSame | centroid | 30/-30, 3.71 | 53 | 0.467 | 0.0e+0 |
| 5 | hitch_receiver/faces/faces_5 | same, IsSame | centroid | -60/35, 2.89 | 65 | 0.124 | 0.0e+0 |
| 6 | hitch_receiver/faces/faces_4 | same, IsSame | centroid | -60/-30, 2.89 | 61 | 0.184 | 0.0e+0 |
| 7 | hitch_receiver/faces/faces_1 | same, IsSame | centroid | 30/-30, 2.89 | 52 | 0.165 | 0.0e+0 |
| 8 | hitch_receiver/faces/faces_2 | same, IsSame | centroid | -60/-30, 2.89 | 52 | 0.409 | 0.0e+0 |
| 9 | touch_display/faces/faces_0 | same, IsSame | centroid | -150/35, 5.92 | 120 | 0.459 | 0.0e+0 |
| 10 | touch_display/faces/faces_2 | same, IsSame | centroid | -105/5, 5.92 | 43 | 0.209 | 0.0e+0 |
| 11 | touch_display/faces/faces_3 | same, IsSame | centroid | 75/5, 5.92 | 43 | 0.209 | 7.1e-15 |
| 12 | touch_display/faces/faces_5 | same, IsSame | centroid | -60/35, 5.92 | 36 | 0.190 | 0.0e+0 |
| 13 | lift_carriage/faces/faces_1 | same, IsSame | centroid | -105/5, 3.50 | 79 | 0.265 | 3.6e-15 |
| 14 | lift_carriage/faces/faces_4 | same, IsSame | centroid | 120/35, 3.50 | 59 | 0.458 | 3.6e-15 |
| 15 | lift_carriage/faces/faces_41 | same, IsSame | centroid | 30/-30, 3.50 | 54 | 0.383 | 0.0e+0 |
| 16 | lift_carriage/faces/faces_47 | same, IsSame | centroid | -60/-30, 3.50 | 51 | 0.239 | 0.0e+0 |
| 17 | fan/faces/faces_3 | same, IsSame | centroid | 75/5, 7.70 | 187 | 0.315 | 0.0e+0 |
| 18 | fan/faces/faces_5 | same, IsSame | centroid | -15/80, 7.70 | 81 | 0.140 | 0.0e+0 |
| 19 | fan/faces/faces_1 | same, IsSame | centroid | 30/35, 7.70 | 70 | 0.304 | 0.0e+0 |
| 20 | fan/faces/faces_4 | same, IsSame | centroid | 120/-30, 7.70 | 45 | 0.271 | 0.0e+0 |

- Python's projection and three.js's agree to 2.3e-12 px, so the camera state crosses the wire
  exactly. The 0.1 to 0.5 px is the click rounded to whole pixels.
- The exact anchor (click ray meets the resolved face) lies 0.02 to 0.19 mm from the aimed point
  (half a pixel at zoom 2.9 to 7.7).
- Part-level picks (double-click with no tool) give `lastPick` = `/robot_assembly/<part>` for 5/5;
  Phase 1's "a pick returns lower_plate" uses this path.
- All 20 faces are planes (M, `BRepAdaptor_Surface.GetType`). Extra check, not one of the 20:
  curved faces only (`--suite curved`, GPU), points sampled on the surface where it faces the camera.
  Six cylinders passed the visibility and edge-margin rules (lidar, three wheels, two hubs; no other
  part offers one), and 6/6 resolve to the intended face, max 0.635 px
  (`results/s1_angle_vulkan_curved.json`; it reports `pass=false` only because I had asked for 8).
- Many parts have no pickable face from outside: battery, compute and the housing panels are
  enclosed or covered (exact ray casts). The 5 parts were the first in a fixed list with 4 or more
  pickable faces.

## Q3. Overlay: pin and arrow at a picked point (M)

`src/overlay.ts` keeps a second three.js `Scene` and draws it with the viewer's own `WebGLRenderer`
(same canvas, same GL context, same camera). The hook is three.js's `scene.onAfterRender` on the
viewer's scene, re-attached after every `Viewer.render()`. It runs right after the CAD pass, while
the CAD depth buffer is still intact, so the overlay is depth-tested against the model. It skips
passes into render targets: the viewer renders the same scene into its GPU pick buffer, and
otherwise the overlay would be drawn into the pick buffer (R, code). In the run the hook drew 16
times and skipped 12 render-target passes (M).
The viewer's own `onAfterRender` would be too late, because `update()` clears depth for the
orientation marker before calling it (R, code: three-cad-viewer `update()`).

Anchor: the exact click-ray/face point and normal from pick 19 (`fan`, face 1, normal +X). Pin
(needle 60 mm, head radius 9 mm, magenta) and arrow (90 mm, cyan), unlit, in colours that occur
nowhere in the model. Pixels are counted straight from the GL drawing buffer.

| View (orbit about the robot's centre, zoom 1.5) | head pixels / expected disc | head centroid vs independent projection |
|---|---|---|
| az 90, el 45 | 377 / 403 | 0.037 px |
| az 0, el 75 | 373 / 403 | 0.013 px |
| az 60, el 15 | 372 / 403 | 0.064 px |

Occlusion: in the view az -160, el -30, exact ray casts (Python) put all 16 samples of head, needle
and arrow behind `rear_wheel` and `housing_rear_panel`. Drawn overlay pixels with depth test:
head 0, needle 0, arrow 0. Control, same view with depth test off: head 375, needle 186, arrow 603,
head centroid 0.072 px from its projection. So the overlay is in view and only depth hides it.
The anchor was chosen by search: the first resolved pick, lowest first, that has three views with
the head fully visible and one fully occluding view. In an earlier run the anchor was the top face
of the stereo camera, the highest point of the robot; none of 90 views hid all of its pin.

Screenshots: `results/q3_glue_{1,2,3}_angle_vulkan.png`, `q3_occluded_angle_vulkan.png`,
`q3_occluded_control_nodepth_angle_vulkan.png`.

## Q4. Performance on the full robot (M)

Python host, fresh process, `show(robot_assembly())`, from the GPU run:

| Step | Value |
|---|---|
| build | 4.18 s |
| tessellate + convert (core `_convert`) | 2.09 s |
| JSON encode / payload | 3.7 ms / 10.25 MB (base64 buffers) |
| websocket send | 13.8 ms |
| measurement backend: BREP encode + load (stock path) | 131 ms, 3.47 MB, 14,622 ids |
| page: JSON.parse / build scene and tree / GPU finish | 6.6 ms / 152 ms / 6.4 ms |
| **show to first frame (cold process)** | **2.28 s** |
| show to first frame, process already tessellated once | 0.27 to 0.58 s |

Orbit benchmark: one camera step per animation frame through `setCameraLocationSettings` (the same
path a trackball drag takes: controls update, then `update(true, true)`, which also notifies the
host every frame), 300 frames for 360 degrees. Timed in the page with `requestAnimationFrame`.
"Sync" adds a 1-pixel `readPixels` per frame to wait for the GPU. The GPU timer is
`EXT_disjoint_timer_query_webgl2`. "New page" is a fresh page (shader compilation, first upload) on a
host that has tessellated once, except GL-EGL 1580x861, which was also a fresh host process.

Note on the uncapped column (`--disable-frame-rate-limit --disable-gpu-vsync`): without vsync,
animation frames come without backpressure from the GPU, so the interval is not the frame cost.
SwiftShader shows it plainly: 7.4 ms intervals for frames that take 1.3 s (M). The frame cost is
the sync column.

| Backend | Canvas | vsync-paced orbit: interval median / p95 / max, mean fps, frames over 25 ms | frame cost with GPU sync: median / p95 | GPU timer: median / p95 | uncapped rAF interval median (see note) | show to first frame: new page / same page |
|---|---|---|---|---|---|---|
| ANGLE/Vulkan (RADV) | 1580x861 | 16.70 / 16.80 / 16.8 ms, 60.0 fps, 0 of 290 over 25 ms | 4.00 / 5.50 ms | 0.85 / 0.94 ms | 2.20 ms | 0.58 s / 0.34 s |
| ANGLE/Vulkan (RADV) | 2540x1301 | 16.70 / 16.70 / 16.8 ms, 60.0 fps, 0 of 290 over 25 ms | 4.20 / 5.50 ms | 0.92 / 1.01 ms | 2.30 ms | 0.32 s / 0.34 s |
| ANGLE/GL-EGL (radeonsi) | 1580x861 | 16.70 / 16.70 / 16.8 ms, 60.0 fps, 0 of 290 over 25 ms | 4.00 / 5.50 ms | 0.76 / 0.84 ms | 2.40 ms | 2.27 s / 0.49 s |
| ANGLE/GL-EGL (radeonsi) | 2540x1301 | 16.70 / 16.80 / 16.8 ms, 60.0 fps, 0 of 290 over 25 ms | 4.30 / 5.80 ms | 0.88 / 0.99 ms | 2.70 ms | 0.32 s / 0.28 s |
| SwiftShader (software) | 1580x861 | 1316.60 / 1416.60 / 2683.3 ms, 0.8 fps, 32 of 32 over 25 ms | 1336.00 / 1357.40 ms | 1332.97 / 1361.09 ms | 7.40 ms | 5.87 s / 6.33 s |
| SwiftShader (software) | 2540x1301 | 1399.90 / 1466.70 / 1500.0 ms, 0.7 fps, 32 of 32 over 25 ms | 1391.30 / 1428.00 ms | 1398.90 / 1487.20 ms | 7.50 ms | 5.56 s / 6.28 s |

Only the default headless path falls back to SwiftShader. `driver/gpu_probe.mjs` found two flag
sets that put headless Chromium on the AMD GPU with no display variables set (M,
`results/gpu_probe.json`):

- `--use-angle=vulkan --enable-features=Vulkan`: "ANGLE (AMD, Vulkan 1.4.354 (AMD Radeon AI PRO
  R9700 (RADV GFX1201)), radv)"
- `--use-gl=angle --use-angle=gl-egl`: "ANGLE (AMD, AMD Radeon AI PRO R9700 (radeonsi gfx1201
  ACO), OpenGL ES 3.2)"

`--use-angle=gl` and `--use-gl=egl` gave no WebGL2 context. `--ozone-platform=headless` makes no
difference.

The 60 fps verdict rests on headless GPU runs. Headless rendering paces animation frames at 60 Hz
(capped runs) and skips the compositor's presentation to a real display. A frame costs 4.0 to 4.3 ms
median and at most 5.8 ms p95 including a full GPU round trip, a quarter to a third of the 16.7 ms
budget, so the margin is about 3x (M for the cost, R for the margin in a window). Whether a visible Wayland window keeps
the same pace is reasoned, not measured. The owner can measure it with one command (window stays
open about 20 s, then closes; results in `results/visible_*.json`):

```
cd ~/work/privat/workbench/spikes/S1-viewer-host && ./run_visible.sh            # also: --size 2560x1440, --angle vulkan|gl-egl
```

The script was checked end to end in headless mode (`--headless 1`):
`results/visible_script_check_headless_vulkan_1600x1000.json`.

## Q5. Incremental update (M)

After the first show, the host rebuilds the robot, diffs each part against what is shown (geometry
fingerprint plus location), and sends only what changed:

| Change | Sent | Host work | Page work | Result |
|---|---|---|---|---|
| lift from plate top 232.4 to 500 mm, move by transform (`nestedGroup.groups[path]` + public `updateBounds`) | 1,152 B, 13 new locations, 0 re-tessellated | build 3.87 s, diff 122 ms, exact model re-placed 42 ms | 22 ms apply, 23 ms send to GPU done | part boxes vs exact geometry 6.1e-6 mm; vs a full re-show 0 mm; a face pick on the moved camera resolves |
| same state by full re-show | 10.25 MB | tessellate 0.40 s (cached) | 122 ms build, 555 ms show to frame | reference |
| lift back by `removePart` + `addPart` (public API only, geometry the page already has) | 1,304 B | as above | 36.5 ms | 58 leaves, boxes vs exact 6.1e-6 mm, pick on the lift carriage resolves |
| `edge_margin` 6 to 16 mm (only `arm_platform` changes), `updatePart` | 183,657 B, 1 part re-tessellated | tessellate 29 ms | 6.7 ms decode + `updatePart` | 58 leaves, box vs exact 6.1e-6 mm, pick on the new plate resolves |

So `addPart`/`updatePart` avoid re-sending everything: a lift move costs 1.2 kB instead of 10.25 MB,
and a one-part change about 1.8 % of the full payload.

Three things went wrong silently on the way. All three are fixed in the host and checked by the
numbers above; `results/early_run_before_fixes_swiftshader_func.json` is a run from before the
fixes (50 parts re-tessellated for a lift move, 164.75 mm box error after re-adding):

1. **The viewer's `loc` is not build123d's `location`.** ocp-tessellate flattens nested locations.
   For parts built as `Pos(c) * Box(...)` (beams, mast, camera, hub, adapters) the leaf's `loc` is
   the box corner frame, off by half the box size. Sending the new absolute `location` misplaced 7
   of 13 lifted parts by up to 164.75 mm (half the camera mast's height). Fix: send `(L_new * L_old^-1) * viewer_loc`, with the
   viewer's `loc`s taken from the tree the host sent.
2. **`TopoDS_Shape.Moved()` returns a plain `TopoDS_Shape`.** Re-placed faces lost their
   `TopoDS_Face` type, and face functions (normal, properties) failed quietly: no pickable faces
   after a move. Fix: `downcast()`.
3. **Tessellating a shape changes its BREP bytes.** BRepMesh clears the TShape "Modified" flag
   (`0111000` to `0101000`), so a BREP hash flagged 50 unchanged parts as changed. Fix: hash the ASCII
   BREP without triangulation and without the 7-character flag lines. 58/58 stable across
   tessellation and fresh builds.

## API notes for Phase 1

What to subclass or wrap (one adapter module per side, as the plan says):

- **JS: subclass `Viewer`** (`class HostViewer extends Viewer`) and pass it to `createPage`.
  `createPage` does not return the viewer instance, so the constructor captures it. Override the
  `@public` `render()` to re-attach the overlay hook to the new scene after every model. In the
  constructor, set `onAfterRender` (frame hook).
- **JS: transport** = `createPage`'s `send(command, message)` callback out, `page.handleMessage(obj)`
  in. Our own message types share the socket. The page attaches pointer and camera to forwarded
  picks.
- **Python: implement `Comms`** (`send_data`, `send_config`, `send_command`, `status`,
  `workspace_config`, `send_backend`), then `Session(comms)`, `Config(session, ())`,
  `Viewer(config).show(...)`. `status()` answers with the page's last status snapshot.
  `workspace_config()` answers with the host's settings plus `_splash`.
- **Python: `ViewerBackend`** for measurements and picks: feed it every status change set; send any
  response to the page as is.

three-cad-viewer members used, by how public they are (5.1.3 `viewer.d.ts`):

| Tagged `@public` (rely on) | TS-public, undocumented (wrap) | Not used |
|---|---|---|
| `getCameraPosition/Quaternion/Target/Zoom`, `setView`, `render`, `clear`, `getImage`, `addPart`, `removePart`, `updatePart`, `updateBounds`, `getState(s)`; exports `decodeInstancedFormat`, `version` | `renderer` (GL context, pixels), `onAfterRender`, `scene` (overlay hook), `camera.getCamera()` (frustum), `setCameraLocationSettings` (camera set, saved views), `update()`, `shapes` (re-add), `nestedGroup.groups[path]` (move by transform), `getOrtho`, `getClipSlider` | `idPicker` and `pickingController` (picks came through notifications), `options.canvas/gl` (external context: not needed, the overlay uses the viewer's renderer), anything with a leading underscore |

Pick notifications, all through the notify callback as `send("status", {...snapshot, ...delta})`:

- `selectedShapeIDs`: measure tools, full sub-shape paths such as
  `/robot_assembly/fan/faces/faces_1`;
- `lastPick`: double-click, part path;
- `selected`: select tool, **indices only** (`"3"`), no part path. Not enough to resolve; Phase 1
  needs its own pick mode or the measure-tool path.

Python members used: public `Comms`, `Session`, `Config`, `Viewer.show`, `ViewerBackend`,
`MessageType`, `codec.default`. Undocumented: `ViewerBackend.model`. Private:
`Viewer._convert(shape, names=, colors=)` to tessellate one part for `updatePart`. It needs
`session.begin({})`/`session.clear()` around it.

Other facts Phase 1 needs (M unless marked):

- **With a tool active the viewer renders every animation frame** (render loop on). With no tool, it
  renders only on demand. So hover picks need a render between pointer move and press.
- On SwiftShader one frame of this robot takes 1.3 to 1.4 s. While a tool is active the page
  handles messages late: the properties response showed 1.3 s after the click.
- **The GPU pick buffer is half resolution, read in a 3x3 window, with vertex over edge over face**
  (R, code). A click within a few pixels of an edge picks the edge; the test kept 14 px or more from
  edges.
- **Glass mode (the stock default) lays the tree over the canvas.** Check `elementFromPoint` before
  clicking.
- **Explode needs its slider (or play) after the button.** The button only sets up the animation.
- **`setCameraLocationSettings` applies a quaternion only with trackball control** (R, code). The
  stock default is trackball.
- The overlay is not drawn in Studio mode (composer passes render to targets) (R, code; not tested).
- **A page that connects after a show gets nothing.** The stock server drops such a model; this host
  raises in `send_data`, and the driver always waits for the page first. Phase 1's hub should keep
  the last model (and the viewer `loc`s) and replay it when a page connects or reconnects (R).
- The host binds 127.0.0.1 only and has no token or Origin check; the plan requires both for Phase 1.

## Limits of this spike

- One run per configuration; each orbit benchmark is 290 timed frames (32 on SwiftShader).
- Headless only. No visible window, no Studio mode, no perspective camera (the Python camera model
  handles one, untested).
- The host serves the page itself and runs in the spike's own environment, which mirrors
  my-robot's lock. The plan's hub/kernel split was not tried.
- Picks were tested on 20 planar faces and 6 cylinders. Edges and vertices were not picked.

## SwiftShader (software GL), the floor

Everything passes on software GL too (M, `results/s1_swiftshader_full.json`, same driver, same
clicks): Q1 7 of 7 stock features; Q2 20/20 picks (max 0.467 px); Q3 glue 0.013 to 0.085 px and 0
overlay pixels in the occluded view (control: 375 head pixels at 0.059 px); Q5 all four updates (box
errors 6.1e-6 mm, the pick after each update resolves).

It is slow, not wrong. One frame of the full robot takes 1.32 to 1.40 s at either canvas size (the
sync and the GPU timer agree), so 0.7 to 0.8 fps. Show to first frame took 5.4 to 8.6 s, about 5 s of
it the first frame. Q2 took 340 s against 7.6 s on the GPU. A lift move took 1.15 s from send to
drawn frame, against 23 ms. With a measure tool active the viewer redraws every frame, so on
SwiftShader the page answers messages and hover picks about a second late.

The default headless path lands on SwiftShader (M, `gpu_probe.json`), and the stale NVIDIA variables
in `~/.zprofile` can push a windowed Chromium onto software paths too (plan, owner actions; R).
Phase 1 should read the renderer string at start and show a banner when it is SwiftShader (R).

## Rerun

```
cd ~/work/privat/workbench/spikes/S1-viewer-host
env -u VIRTUAL_ENV uv sync && pnpm install --frozen-lockfile && pnpm run build
node driver/gpu_probe.mjs                                  # which flags reach the GPU headless
node driver/run_s1.mjs --backend angle_vulkan --suite full # Q1-Q5 + perf, about 2 min
node driver/run_s1.mjs --backend angle_gl_egl --suite perf
node driver/run_s1.mjs --backend angle_vulkan --suite curved  # extra: picks on curved faces
node driver/run_s1.mjs --backend swiftshader --suite full  # the floor, about 18 min
node driver/summarize.mjs                                  # the numbers above, from results/*.json
./run_visible.sh                                           # owner only: visible window on the GPU
```

The robot snapshot lives in the session scratchpad (`robot-snap`, from
`git archive 936dffa`). `pyproject.toml` points at it, so the scratchpad must exist (or be recreated
the same way) for `uv sync` to work.

## Independent review (2026-10-14)

A fresh reviewer re-ran the full suite with the spike's unmodified host and drivers (copied only
to redirect output) on the AMD GPU (ANGLE Vulkan, RADV). All four claims confirmed; none refuted.
Measured by the reviewer: 20 of 20 picks pass, worst projection error 0.467 px, point on face
within 7.1e-15 mm, page and independent Python projection agree to 2.3e-12 px; pin centroid
0.013 to 0.064 px from its projection in three orbits, 0 overlay pixels when occluded against
375/186/603 with the depth test off; 60.0 fps with interval p95 16.7 to 16.8 ms and 0 of 290
frames over 25 ms at both sizes, forced-sync frame cost median 4.0 to 4.1 ms; a lift move sends
1,153 bytes and places parts exactly where a full re-show does.

Notes from the review:

- `check_pick()`'s `IsSame` test is redundant with the id match in every path exercised, so it
  adds no independent evidence; the point-on-face and projection checks are the real ones.
- All picked faces were easy (large, facing the camera, away from edges); edges and vertices
  were never picked. Phase 1 should test those.
- Not measured: a visible window (`run_visible.sh`), the SwiftShader floor, the cylinder suite.
