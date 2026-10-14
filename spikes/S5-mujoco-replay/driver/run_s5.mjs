// Spike S5 driver: the robot's MuJoCo scene in the STOCK ocp_viewer page (ocp_viewer 1.2.0 server
// on port 3959), measured headless through CDP.
//
//   node driver/run_s5.mjs --gl=swiftshader[,vulkan,egl] [--sections=load,play,poses,pick,slider]
//
// Needs the recordings made by the host's `record` op (results/recording_*.npz).
// Starts and always stops: the ocp_viewer server, the Python host, headless Chromium.

import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { RESULTS, SCRATCH, SPIKE, sleep, startChrome, startHost, startServer, stats, stopChild, waitUntil, writeJson } from "./lib.mjs";

const PORT = 3959;
const CDP = 9359;
const VIEWER = `http://127.0.0.1:${PORT}/viewer`;
const GL = {
    swiftshader: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    vulkan: ["--use-angle=vulkan", "--enable-features=Vulkan", "--ignore-gpu-blocklist"],
    egl: ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist"],
};
const arg = (name, dflt) => process.argv.slice(2).find((a) => a.startsWith(`--${name}=`))?.split("=")[1] ?? dflt;
const gls = arg("gl", "swiftshader").split(",");
const sections = new Set(arg("sections", "load,play,poses,pick,slider").split(","));
const [WIDTH, HEIGHT] = arg("size", "1600x1000").split("x").map(Number);
const SUFFIX = WIDTH === 1600 && HEIGHT === 1000 ? "" : `_${WIDTH}x${HEIGHT}`;
const SMOOTH = { medianMs: 17.5, p95Ms: 20, maxMs: 50, clockRateTol: 0.02 }; // fixed before measuring

const logLines = [];
const log = (s) => {
    logLines.push(`${new Date().toISOString()} ${s}`);
    console.log(s);
};

async function waitEvent(p, pred, what, timeoutMs = 120000) {
    return waitUntil(() => p.eval(`(() => __s5.events.find((e) => e.done && (${pred})) ?? null)()`), { timeoutMs, intervalMs: 20, what });
}

async function show(p, host, args) {
    const r = await host.call("show", args);
    const ev = await waitEvent(p, `e.kind === "data" && e.s5 && e.s5.tag === ${JSON.stringify(args.tag)}`, `show ${args.tag}`);
    const render = await p.eval(`__s5.events.filter((e) => e.kind === "viewer.render").slice(-1)[0] ?? null`);
    const counts = await p.eval("__s5.sceneCounts()");
    const row = {
        tag: args.tag, which: args.which, primitives: args.primitives,
        python: { build_s: r.build_s, config_s: r.config_s, dumps_s: r.dumps_s, wire_bytes: r.wire_bytes, send_ms: r.t_send_end_wall_ms - r.t_send_start_wall_ms },
        page: {
            recv_after_send_start_ms: ev.recvWall - r.t_send_start_wall_ms,
            handle_ms: ev.handledWall - ev.recvWall,
            viewer_render_ms: render ? render.endWall - render.startWall : null,
            gpu_ms: ev.gpuDoneWall - ev.handledWall,
            raf_after_gpu_ms: ev.rafWall - ev.gpuDoneWall,
        },
        total_ms: ev.rafWall - r.t_start_wall_ms, // conversion start in Python -> first frame after the scene is drawn
        from_send_ms: ev.rafWall - r.t_send_start_wall_ms,
        counts, stats: r.stats,
    };
    log(`  show ${args.tag}: total ${row.total_ms.toFixed(0)} ms (python build ${(r.build_s * 1000).toFixed(0)} config ${(r.config_s * 1000).toFixed(0)} send ${row.python.send_ms.toFixed(0)}; page recv ${row.page.recv_after_send_start_ms.toFixed(0)} handle ${row.page.handle_ms.toFixed(0)} gpu ${row.page.gpu_ms.toFixed(0)} raf ${row.page.raf_after_gpu_ms.toFixed(0)}) wire ${(r.wire_bytes / 1e6).toFixed(2)} MB, ${counts.frontMeshes} meshes ${counts.frontTriangles} tris`);
    return { row, root: r.root, bodyPaths: Object.values(r.body_paths) };
}

async function loadSection(p, host, gl) {
    const rows = [];
    const variants = [
        ["visual", "direct"], ["visual", "direct"], ["visual", "direct"],
        ["visual", "brep"], ["collision", "direct"], ["collision", "brep"],
    ];
    for (const [i, [which, primitives]] of variants.entries()) {
        const s = await show(p, host, { which, primitives, tag: `load-${i}-${which}-${primitives}` });
        rows.push(s.row);
        if (i === 0 || (which === "collision" && primitives === "direct")) await p.screenshot(path.join(RESULTS, `load_${which}_${gl}${SUFFIX}.png`));
    }
    const visual = rows.filter((r) => r.which === "visual");
    return { rows, pass_under_5s: visual.every((r) => r.total_ms < 5000), worst_visual_total_ms: Math.max(...visual.map((r) => r.total_ms)) };
}

async function clickAt(p, x, y, clickCount = 1) {
    await p.mouse("mouseMoved", x, y);
    await p.mouse("mousePressed", x, y, { buttons: 1, clickCount });
    await p.mouse("mouseReleased", x, y, { buttons: 0, clickCount });
}

function clockRate(actionTimes, duration) {
    // clipAction.time per wall second, unwrapping the loop
    let offset = 0;
    const t = [];
    for (let i = 0; i < actionTimes.length; i++) {
        const v = actionTimes[i][1];
        if (i && v + offset < t[i - 1] - duration / 2) offset += duration;
        t.push(v + offset);
    }
    const n = t.length;
    return n > 2 ? (t[n - 1] - t[1]) / ((actionTimes[n - 1][0] - actionTimes[1][0]) / 1000) : null;
}

async function playSection(p, host, gl) {
    const out = {};
    for (const [which, run] of [["visual", "swing"], ["collision", "swing"], ["visual", "trip_above"]]) {
        await show(p, host, { which, primitives: "direct", tag: `play-show-${which}-${run}` });
        const an = await host.call("animate", { run, speed: 1.0, tag: `play-anim-${which}-${run}` });
        const ev = await waitEvent(p, `e.kind === "animation" && e.s5 && e.s5.tag === "play-anim-${which}-${run}"`, "animation");
        const info = await p.eval("__s5.animInfo()");
        const btn = await p.eval("__s5.clickPlay()");
        let how = "ui button";
        if (btn.ok && btn.visible) await clickAt(p, btn.x, btn.y);
        else {
            how = "controlAnimation('play')";
            await p.eval("__s5.viewer.controlAnimation('play')");
        }
        await sleep(300);
        const running = await p.eval("__s5.animInfo().running");
        const fr = await p.eval("__s5.measureFrames(10000)", { timeoutMs: 90000 });
        const iv = fr.intervals.slice(5);
        const st = stats(iv);
        const rate = clockRate(fr.actionTimes, info.duration);
        const res = {
            run, which, how, running, anim: { ...an, track_paths: undefined }, animRecv: { handle_ms: ev.handledWall - ev.recvWall, recv_after_send_ms: ev.recvWall - an.t_send_start_wall_ms },
            info, frames: fr.intervals.length, elapsedMs: fr.elapsedMs, interval: st, fps: st ? 1000 / st.mean : null,
            over25ms: iv.filter((x) => x > 25).length, over50ms: iv.filter((x) => x > 50).length,
            updateCpu: stats(fr.updateMs), render: fr.render, clockRate: rate,
            smooth: !!st && st.median <= SMOOTH.medianMs && st.p95 <= SMOOTH.p95Ms && st.max <= SMOOTH.maxMs && rate !== null && Math.abs(rate - 1) <= SMOOTH.clockRateTol,
        };
        log(`  play ${which}/${run} (${how}, running=${running}): ${res.frames} frames in ${(fr.elapsedMs / 1000).toFixed(1)} s, median ${st?.median.toFixed(1)} p95 ${st?.p95.toFixed(1)} max ${st?.max.toFixed(1)} ms, fps ${res.fps?.toFixed(1)}, clock ${rate?.toFixed(3)}, update cpu median ${res.updateCpu?.median.toFixed(1)} ms -> smooth ${res.smooth}`);
        await p.eval("__s5.viewer.controlAnimation('pause')");
        await p.screenshot(path.join(RESULTS, `play_${which}_${run}_${gl}${SUFFIX}.png`));
        await p.eval("__s5.viewer.controlAnimation('stop')");
        out[`${which}_${run}`] = res;
    }
    out.criteria = SMOOTH;
    return out;
}

// Five more 10 s playbacks of the visual swing on one scene, to count hitches rather than rely on one run.
async function repeatSection(p, host, gl) {
    await show(p, host, { which: "visual", primitives: "direct", tag: "repeat-show" });
    await host.call("animate", { run: "swing", speed: 1.0, tag: "repeat-anim" });
    await waitEvent(p, `e.kind === "animation" && e.s5 && e.s5.tag === "repeat-anim"`, "animation");
    const info = await p.eval("__s5.animInfo()");
    const btn = await p.eval("__s5.clickPlay()");
    await clickAt(p, btn.x, btn.y);
    await sleep(300);
    const reps = [];
    for (let i = 0; i < 5; i++) {
        const fr = await p.eval("__s5.measureFrames(10000)", { timeoutMs: 90000 });
        const iv = fr.intervals.slice(5);
        reps.push({ frames: fr.intervals.length, interval: stats(iv), over20ms: iv.filter((x) => x > 20).length, over25ms: iv.filter((x) => x > 25).length,
            over50ms: iv.filter((x) => x > 50).length, clockRate: clockRate(fr.actionTimes, info.duration) });
    }
    await p.eval("__s5.viewer.controlAnimation('stop')");
    const total = reps.reduce((a, r) => a + r.frames, 0);
    const res = { reps, totalFrames: total, totalOver20ms: reps.reduce((a, r) => a + r.over20ms, 0), totalOver50ms: reps.reduce((a, r) => a + r.over50ms, 0),
        worstMs: Math.max(...reps.map((r) => r.interval.max)), smoothRuns: reps.filter((r) => r.interval.median <= SMOOTH.medianMs && r.interval.p95 <= SMOOTH.p95Ms && r.interval.max <= SMOOTH.maxMs && Math.abs(r.clockRate - 1) <= SMOOTH.clockRateTol).length };
    log(`  repeat visual/swing 5 x 10 s: ${total} frames, ${res.totalOver20ms} over 20 ms, ${res.totalOver50ms} over 50 ms, worst ${res.worstMs.toFixed(1)} ms, smooth runs ${res.smoothRuns}/5`);
    return res;
}

// Two recordings in a row on one scene: (a) as the stock messages allow (second animation message
// on top of the first), (b) with the page calling the viewer's own controlAnimation('stop') and
// clearAnimation() in between - JS methods no stock message reaches.
async function clearSection(p, host, gl) {
    const out = {};
    for (const mode of ["stock", "page_clear"]) {
        const s = await show(p, host, { which: "visual", primitives: "direct", tag: `clear-show-${mode}` });
        await host.call("animate", { run: "swing", speed: 1.0, tag: `clear-a-${mode}` });
        await waitEvent(p, `e.kind === "animation" && e.s5 && e.s5.tag === "clear-a-${mode}"`, "first animation");
        await p.eval("__s5.viewer.setRelativeTime(0.7)"); // the first recording has been played/scrubbed
        if (mode === "page_clear") await p.eval("(() => { __s5.viewer.controlAnimation('stop'); __s5.viewer.clearAnimation(); return true; })()");
        await host.call("animate", { run: "trip_above", speed: 1.0, tag: `clear-b-${mode}` });
        await waitEvent(p, `e.kind === "animation" && e.s5 && e.s5.tag === "clear-b-${mode}"`, "second animation");
        const info = await p.eval("__s5.animInfo()");
        const indices = evenly(195, 20); // trip_above has 195 samples
        const readings = await p.eval(`(() => {
            const a = __s5.viewer.animation, times = a.clip.tracks.find((t) => t.times.length === 195).times;
            return ${JSON.stringify(indices)}.map((k) => { __s5.viewer.setRelativeTime(times[k] / a.duration); return { k, tPage: times[k], poses: __s5.worldPoses(${JSON.stringify(s.bodyPaths)}) }; });
        })()`);
        const cmp = await host.call("compare", { run: "trip_above", readings });
        out[mode] = { tracksLoaded: info.tracks, clipDuration: info.duration, max_pos_err_m: cmp.max_pos_err_m, max_angle_err_rad: cmp.max_angle_err_rad };
        log(`  second recording after the first, ${mode}: ${info.tracks} tracks loaded, max pos err ${cmp.max_pos_err_m.toExponential(2)} m`);
    }
    return out;
}

function evenly(n, count) {
    const idx = new Set();
    for (let i = 0; i < count; i++) idx.add(Math.round((i * (n - 1)) / (count - 1)));
    return [...idx];
}

async function posesSection(p, host, gl) {
    const out = {};
    // "swing_helper": the same swing sent by ocp_viewer's own Animation helper instead of our message
    for (const [label, run, via] of [["swing", "swing", "message"], ["trip_above", "trip_above", "message"], ["trip_below", "trip_below", "message"], ["swing_helper", "swing", "helper"]]) {
        const s = await show(p, host, { which: "visual", primitives: "direct", tag: `poses-show-${label}` });
        if (via === "helper") {
            const n0 = await p.eval("__s5.events.length");
            out[`${label}_send`] = await host.call("animate_helper", { run, speed: 1.0 });
            await waitUntil(() => p.eval(`__s5.events.slice(${n0}).some((e) => e.done && e.kind === "animation")`), { timeoutMs: 60000, what: "helper animation" });
        } else {
            await host.call("animate", { run, speed: 1.0, tag: `poses-anim-${label}` });
            await waitEvent(p, `e.kind === "animation" && e.s5 && e.s5.tag === "poses-anim-${label}"`, "animation");
        }
        const info = await p.eval("__s5.animInfo()");
        const indices = evenly(info.keyframes, 20);
        const leafPaths = await p.eval(`__s5.leafPaths(${JSON.stringify(s.root + "/")})`);
        const readings = await p.eval(`__s5.sampleKeyframes(${JSON.stringify(indices)}, ${JSON.stringify(s.bodyPaths)}, ${JSON.stringify(leafPaths)})`);
        const cmp = await host.call("compare", { run, readings });
        const leaves = await host.call("compare_leaves", { run, readings });
        const root = await p.eval("__s5.rootWorld()");
        const scale = Math.max(...readings.flatMap((r) => Object.values(r.poses).map((q) => Math.max(...q.s.map((v) => Math.abs(v - 1))))));
        out[label] = { via, info, indices, ...cmp, leaves, rootWorldMatrix: root, maxScaleDeviation: scale };
        log(`  poses ${label} (${via}): ${cmp.samples} times x ${cmp.body_poses_compared / cmp.samples} bodies, max pos err ${cmp.max_pos_err_m.toExponential(2)} m, max angle err ${cmp.max_angle_err_rad.toExponential(2)} rad -> pass ${cmp["pass_1e-6_m"]}; ${leaves.leaf_poses_compared} leaf poses vs geom_xpos: max ${leaves.max_leaf_pos_err_m.toExponential(2)} m, rotmat ${leaves.max_leaf_rotmat_err.toExponential(2)}`);
        if (via === "message") {
            await p.eval("__s5.viewer.setRelativeTime(0.5)");
            await p.screenshot(path.join(RESULTS, `poses_${label}_mid_${gl}${SUFFIX}.png`));
            await p.eval("__s5.viewer.setRelativeTime(1.0)");
            await p.screenshot(path.join(RESULTS, `poses_${label}_end_${gl}${SUFFIX}.png`));
        }
    }
    return out;
}

async function pickSection(p, host, gl) {
    const s = await show(p, host, { which: "visual", primitives: "direct", tag: "pick-show" });
    const centers = await p.eval(`__s5.leafCenters(${JSON.stringify(s.root + "/")})`);
    const ids = Object.keys(centers);
    const proj = await p.eval(`__s5.project(${JSON.stringify(ids.map((id) => centers[id].c))})`);
    const rect = await p.eval("__s5.canvasRect()");
    const cands = [];
    for (let i = 0; i < ids.length; i++) {
        const x = Math.round(proj[i][0]), y = Math.round(proj[i][1]);
        if (x < rect.x + 8 || x > rect.x + rect.w - 8 || y < rect.y + 8 || y > rect.y + rect.h - 8) continue;
        const el = await p.eval(`__s5.elementAt(${x}, ${y})`);
        if (!el.isCanvas) continue;
        const hit = await p.eval(`__s5.raycast(${x}, ${y})`);
        if (hit && hit.id === ids[i]) {
            const [path_, name] = [ids[i].slice(0, ids[i].lastIndexOf("/")), ids[i].slice(ids[i].lastIndexOf("/") + 1)];
            const info = await host.call("map_pick", { path: path_, name });
            const sizePx = await p.eval(`__s5.leafScreenSize(${JSON.stringify(ids[i])})`);
            cands.push({ id: ids[i], x, y, kind: info?.kind, type: info?.type, secondId: hit.secondId, secondGapMm: hit.secondGapMm, sizePx });
        }
    }
    // a mix of mesh and primitive leaves
    const meshes = cands.filter((c) => c.kind === "mesh"), prims = cands.filter((c) => c.kind !== "mesh");
    const chosen = [];
    while (meshes.length || prims.length) {  // every candidate, alternating kinds
        if (meshes.length) chosen.push(meshes.shift());
        if (prims.length) chosen.push(prims.shift());
    }
    const picks = [];
    for (const c of chosen) {
        const o0 = await p.eval("__s5.outgoing.length");
        await p.mouse("mouseMoved", c.x - 3, c.y - 2);
        await p.mouse("mouseMoved", c.x, c.y);
        await sleep(80);
        await p.eval("__s5.viewer.update(true, false)");
        await sleep(40);
        await clickAt(p, c.x, c.y, 1);
        await clickAt(p, c.x, c.y, 2);
        const lp = await waitUntil(() => p.eval(`__s5.outgoing.slice(${o0}).find((r) => r.lastPick) ?? null`), { timeoutMs: 5000, what: "lastPick" }).catch(() => null);
        const got = lp ? `${lp.lastPick.path}/${lp.lastPick.name}` : null;
        const mapped = lp ? await host.call("map_pick", lp.lastPick) : null;
        picks.push({ expected: c.id, kind: c.kind, type: c.type, px: [c.x, c.y], sizePx: c.sizePx, raycastSecond: c.secondId, raycastSecondGapMm: c.secondGapMm,
            got, mapped, pass: got === c.id && !!mapped && mapped.consistent, mapsBack: !!mapped && mapped.consistent });
        await p.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
        await p.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
        await sleep(50);
    }
    const status = await host.call("status");
    const last = picks.filter((k) => k.got).slice(-1)[0];
    const res = {
        candidatesVisibleAtCentre: cands.length, picks, nPass: picks.filter((k) => k.pass).length, nMapBack: picks.filter((k) => k.mapsBack).length,
        pythonStatusLastPick: status.lastPick ? { path: status.lastPick.path, name: status.lastPick.name } : null,
        pythonSeesLastPick: !!last && !!status.lastPick && `${status.lastPick.path}/${status.lastPick.name}` === last.got,
    };
    log(`  picks: ${res.nPass}/${picks.length} resolved to the raycast leaf and mapped to geom/body; python status lastPick ${JSON.stringify(res.pythonStatusLastPick)} matches: ${res.pythonSeesLastPick}`);
    await p.screenshot(path.join(RESULTS, `pick_${gl}${SUFFIX}.png`));
    return res;
}

async function sliderSection(p, host, gl) {
    const out = {};
    // Each mechanism starts from a fresh show (groups at the scene's base pose, no tracks loaded).
    // "direct_then_animation" is one direct update followed by animation updates on the same scene:
    // the viewer adds a track's values to the group's pose *when the track is added*.
    const plan = [["direct", 20], ["direct_held", 20], ["animation", 4], ["direct_then_animation", 3], ["reshow", 3]];
    let s = null;
    for (const [name, n] of plan) {
        s = await show(p, host, { which: "visual", primitives: "direct", reset_camera: "keep", tag: `slider-show-${name}` });
        const rows = [];
        for (let i = 0; i < n; i++) {
            const mech = name === "direct_then_animation" ? (i === 0 ? "direct" : "animation") : name;
            const value = (i % 2 ? -0.6 : 0.6) * (1 + 0.01 * i);
            const r = await host.call("slider", { mechanism: mech, joint: "left_shoulder_lift", value, seq: i, series: name });
            const pred = mech === "animation" ? `e.kind === "set_relative_time" && e.s5 && e.s5.tag === "${r.tag}"` : `e.s5 && e.s5.tag === "${r.tag}"`;
            const ev = await waitEvent(p, pred, r.tag, 60000);
            const poses = await p.eval(`__s5.worldPoses(${JSON.stringify(s.bodyPaths)})`);
            const err = await host.call("compare_poses", { expected: r.expected, poses });
            const anim = await p.eval("(() => { const i = __s5.animInfo(); return { tracks: i.tracks, clipTracks: i.clipTracks }; })()");
            rows.push({
                i, mech, value, mj_forward_ms: r.mj_forward_ms, build_ms: r.build_ms, send_ms: r.t_send_end_wall_ms - r.t_send_start_wall_ms,
                recv_ms: ev.recvWall - r.t0_wall_ms, handled_ms: ev.handledWall - r.t0_wall_ms, gpu_ms: ev.gpuDoneWall - r.t0_wall_ms,
                shown_ms: ev.rafWall - r.t0_wall_ms, apply_ms: ev.applyMs ?? null, animTracksLoaded: anim.tracks, ...err,
            });
            await sleep(30);
        }
        const shown = stats(rows.map((r) => r.shown_ms));
        out[name] = { rows, shown, gpu: stats(rows.map((r) => r.gpu_ms)), maxPosErrM: Math.max(...rows.map((r) => r.max_pos_err_m)), posErrByUpdate: rows.map((r) => r.max_pos_err_m) };
        log(`  slider ${name}: shown median ${shown.median.toFixed(1)} ms p95 ${shown.p95.toFixed(1)} max ${shown.max.toFixed(1)}; pose err by update ${rows.map((r) => r.max_pos_err_m.toExponential(1)).join(" ")} m`);
    }
    await p.screenshot(path.join(RESULTS, `slider_${gl}${SUFFIX}.png`));
    return out;
}

/**
 * The stock page keeps its Viewer in createPage's closure. Two ways in, both without changing the
 * page: (1) the injected harness wraps Viewer.prototype.render of the same module instance and
 * captures `this` on the next render - if the import resolved after the onload splash render, the
 * page's own global `showViewer()` re-renders the splash; (2) CDP Runtime.queryObjects on
 * Viewer.prototype lists the live instances.
 */
async function viewerAccess(p) {
    const out = { capturedByPatch: false, neededSplashRerender: false };
    try {
        await waitUntil(() => p.eval("!!(window.__s5 && __s5.viewer)"), { timeoutMs: 5000, what: "viewer captured" });
        out.capturedByPatch = true;
    } catch {
        out.neededSplashRerender = true;
        await p.eval("window.showViewer()");
        await waitUntil(() => p.eval("!!(window.__s5 && __s5.viewer)"), { timeoutMs: 20000, what: "viewer captured after showViewer()" });
        out.capturedByPatch = true;
    }
    const proto = await p.send("Runtime.evaluate", { expression: "import('/static/js/three-cad-viewer.esm.js').then((m) => m.Viewer.prototype)", awaitPromise: true });
    const found = await p.send("Runtime.queryObjects", { prototypeObjectId: proto.result.objectId });
    const cmp = await p.send("Runtime.callFunctionOn", {
        objectId: found.objects.objectId,
        functionDeclaration: "function () { return { instances: this.length, sameAsPatch: this.length === 1 && this[0] === window.__s5.viewer }; }",
        returnByValue: true,
    });
    out.queryObjects = cmp.result.value;
    return out;
}

async function runGl(gl, server, host) {
    const res = { gl, flags: GL[gl] };
    log(`== ${gl} ${WIDTH}x${HEIGHT}`);
    const chrome = await startChrome({ width: WIDTH, height: HEIGHT, cdpPort: CDP, flags: GL[gl], profileDir: path.join(SCRATCH, `chrome-${gl}`) });
    const p = chrome.page;
    try {
        await p.send("Page.addScriptToEvaluateOnNewDocument", { source: readFileSync(path.join(SPIKE, "driver", "harness.js"), "utf8") });
        const reg0 = server.registered;
        const loaded = p.waitFor("Page.loadEventFired", 30000);
        await p.send("Page.navigate", { url: VIEWER });
        await loaded;
        await waitUntil(() => server.registered > reg0, { timeoutMs: 20000, what: "browser registration" });
        res.viewerAccess = await viewerAccess(p);
        log(`  viewer access: ${JSON.stringify(res.viewerAccess)}`);
        res.chrome = chrome.version.Browser;
        res.glInfo = await p.eval("__s5.glInfo()");
        log(`  GL: ${res.glInfo.renderer} canvas ${res.glInfo.canvas}`);
        if (sections.has("load")) res.load = await loadSection(p, host, gl);
        if (sections.has("play")) res.play = await playSection(p, host, gl);
        if (sections.has("poses")) res.poses = await posesSection(p, host, gl);
        if (sections.has("pick")) res.pick = await pickSection(p, host, gl);
        if (sections.has("slider")) res.slider = await sliderSection(p, host, gl);
        if (sections.has("repeat")) res.repeat = await repeatSection(p, host, gl);
        if (sections.has("clear")) res.clear = await clearSection(p, host, gl);
    } catch (e) {
        res.error = String(e.stack ?? e);
        log(`ERROR ${gl}: ${res.error}`);
    } finally {
        res.console = p.console.slice(0, 60);
        p.close();
        await stopChild(chrome.child);
    }
    return res;
}

async function main() {
    const out = { started: new Date().toISOString(), port: PORT, gl: {} };
    const server = startServer(PORT, (l) => logLines.push(l));
    let host;
    try {
        await waitUntil(() => server.lines.some((l) => l.line.includes("runs at")) || server.child.exitCode !== null, { timeoutMs: 60000, what: "server" });
        if (server.child.exitCode !== null) throw new Error(`server exited: ${server.lines.map((l) => l.line).join("\n")}`);
        host = startHost(PORT, (l) => logLines.push(l));
        await host.ready();
        out.init = await host.call("init", { payload: 0.15 });
        out.runs = await host.call("load_runs");
        log(`host: ${JSON.stringify(out.init)} runs ${JSON.stringify(out.runs)}`);
        for (const gl of gls) {
            out.gl[gl] = await runGl(gl, server, host);
            writeJson(`s5_${gl}${SUFFIX}.json`, { init: out.init, runs: out.runs, window: [WIDTH, HEIGHT], sections: [...sections], ...out.gl[gl] });
        }
    } finally {
        if (host) {
            await host.call("close", {}, { timeoutMs: 5000 }).catch(() => {});
            await stopChild(host.child);
        }
        await stopChild(server.child, { first: "SIGINT" });
        out.serverLog = server.lines.slice(-30).map((l) => l.line);
        writeFileSync(path.join(RESULTS, `driver_log_${gls.join("_")}${SUFFIX}.txt`), logLines.join("\n"));
    }
    return out;
}

main().then(
    () => process.exit(0),
    (e) => {
        console.error("FAILED", e);
        process.exit(1);
    },
);
