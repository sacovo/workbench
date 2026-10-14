// Spike S1 driver: Q1 (stock UI in our page), Q2 (20 face picks resolved in Python), Q3 (overlay
// glued and occluded), Q4 (timings and frame rates), Q5 (incremental updates).
//
//   node driver/run_s1.mjs --backend swiftshader --suite func      # Q1, Q2, Q3, Q5 at 1600x1000
//   node driver/run_s1.mjs --backend angle_vulkan --suite full     # func + perf at both sizes
//   node driver/run_s1.mjs --backend angle_gl_egl --suite perf     # perf only
//
// Starts the Python host and headless Chromium itself (no display variables, so no window can
// open), and stops both process groups at the end. Writes results/s1_<backend>_<suite>.json.

import path from "node:path";
import { RESULTS, SPIKE, controlClient, sleep, startChrome, startHost, stopAll, stopChild, waitUntil, writeJson } from "./lib.mjs";
import { FLAG_SETS } from "./gpu_probe.mjs";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
const BACKEND = args.backend ?? "swiftshader";
const SUITE = args.suite ?? "full";
const SIZES = (args.sizes ?? "1600x1000,2560x1440").split(",").map((s) => s.split("x").map(Number));
const PORT = Number(args.port ?? 8765);
const CDP = Number(args.cdp ?? 9333);
const UNCAPPED = ["--disable-frame-rate-limit", "--disable-gpu-vsync"];
const ROOT = "/robot_assembly";
const OUTFILE = `s1_${BACKEND}_${SUITE}.json`;
// software GL needs about a second per frame on this model, so it gets fewer frames
const FRAMES = Number(args.frames ?? (BACKEND === "swiftshader" ? 40 : 300));
const SYNC_FRAMES = Math.max(10, Math.round(FRAMES / 2.5));

const out = { backend: BACKEND, flags: FLAG_SETS[BACKEND], suite: SUITE, started: new Date().toISOString(), sections: {} };
const save = () => writeJson(OUTFILE, out);
const hostLog = [];

const sub = (a, b) => a.map((x, i) => x - b[i]);
const add = (a, b) => a.map((x, i) => x + b[i]);
const mul = (a, s) => a.map((x) => x * s);
const norm = (a) => mul(a, 1 / Math.hypot(...a));
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dist2 = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const numbersIn = (text) => (text.match(/-?\d+(?:\.\d+)?(?:e-?\d+)?/gi) ?? []).map(Number);

async function section(name, fn) {
    const t0 = Date.now();
    console.log(`== ${name}`);
    try {
        out.sections[name] = { ok: true, ...(await fn()) };
    } catch (e) {
        out.sections[name] = { ...(out.sections[name] ?? {}), ok: false, error: String(e.stack ?? e) };
        console.error(`${name} FAILED:`, e);
    }
    out.sections[name].elapsedMs = Date.now() - t0;
    console.log(`   pass=${out.sections[name].pass} (${out.sections[name].elapsedMs} ms)`);
    save();
    return out.sections[name];
}

let chrome = null;
let ctl = null;

async function launch(width, height, uncapped) {
    if (chrome) await stopChild(chrome.child);
    const log = [];
    const flags = [...FLAG_SETS[BACKEND], ...(uncapped ? UNCAPPED : [])];
    chrome = await startChrome({ url: `http://127.0.0.1:${PORT}/`, width, height, cdpPort: CDP, flags, profileDir: path.join(SPIKE, ".chrome-profile"), log });
    chrome.log = log;
    await ctl.call("wait_page", { timeout: 30 });
    await waitUntil(() => chrome.page.eval("!!window.__s1 && !!window.__s1.booted"), { what: "page boot" });
    return chrome;
}

/** Show the robot and wait for the page to report the frame; returns host and page timings. */
async function showAndWait(showArgs = {}) {
    const p = chrome.page;
    const n0 = await p.eval("__s1.timings.length");
    const host = await ctl.call("show_robot", showArgs);
    const page = await waitUntil(async () => {
        const n = await p.eval("__s1.timings.length");
        return n > n0 ? p.eval(`__s1.timings[${n - 1}]`) : null;
    }, { timeoutMs: 180000, what: "model shown" });
    return { host, page, showToFirstFrameMs: page.firstFrameWallMs - host.show_start_wall_ms, sendToRecvMs: page.recvWallMs - host.send_start_wall_ms };
}

async function frames() {
    return chrome.page.eval("__s1.frames");
}

/** Render `n` frames, each on its own animation frame (hover picking happens inside a render, and
 * with no tool active the viewer renders only on demand). */
async function waitFrames(n = 2, timeoutMs = 60000) {
    const f0 = await frames();
    for (let i = 0; i < n; i++) {
        await chrome.page.eval("new Promise((r) => requestAnimationFrame(() => { __s1.viewer.update(true, false); requestAnimationFrame(() => r(true)); }))", { timeoutMs });
    }
    const f1 = await frames();
    if (f1 < f0 + n) throw new Error(`expected ${n} renders, saw ${f1 - f0}`);
}

/** A click on the canvas as a user does it: move there, let the viewer render the hover, press, release. */
async function canvasClick(x, y, { clickCount = 1 } = {}) {
    const p = chrome.page;
    await p.move(x - 4, y - 3);
    await p.move(x, y);
    await waitFrames(2);
    await p.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount });
    await p.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount });
}

async function clickButton(name) {
    const r = await chrome.page.eval(`__s1.rectOf(".tcv_button_${name}")`);
    if (!r || !r.visible) throw new Error(`button ${name} not visible`);
    await chrome.page.click(r.x, r.y, { settleMs: 30 });
    await sleep(300);
    await waitFrames(1);
    return r;
}

async function pageProject(points) {
    return chrome.page.eval(`__s1.project(${JSON.stringify(points)})`);
}

async function setView(view) {
    return chrome.page.eval(`__s1.setView(${JSON.stringify(view.position)}, ${JSON.stringify(view.target)}, ${view.zoom})`);
}

function viewAround(target, az, el, distance, zoom) {
    const a = (az * Math.PI) / 180, e = (el * Math.PI) / 180;
    return { position: add(target, mul([Math.cos(e) * Math.cos(a), Math.cos(e) * Math.sin(a), Math.sin(e)], distance)), target, zoom, az, el };
}

let frustumH = null;
let robotCentre = null;

async function sceneGeometry() {
    const cam = await chrome.page.eval("__s1.cameraState()");
    frustumH = cam.top - cam.bottom;
    robotCentre = cam.target;
}

/** Faces of `part` that are visible, well inside their boundary on screen, from views aimed at the part. */
async function findCandidates(part, needed = 4, surface = "any") {
    const [b] = Object.values(await ctl.call("part_world_bbox", { labels: [part] }));
    const centre = [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2];
    const size = Math.max(b[3] - b[0], b[4] - b[1], b[5] - b[2]);
    const zoom = Math.min(12, Math.max(1, (0.45 * frustumH) / size));
    const found = new Map();
    const tried = [];
    for (const [az, el] of [[-60, 35], [30, 35], [120, 35], [-150, 35], [-60, -30], [30, -30], [120, -30], [-150, -30], [-15, 80], [-105, 5], [75, 5]]) {
        const view = viewAround(centre, az, el, 2500, zoom);
        const cam = await setView(view);
        const pxPerMm = cam.canvas.height / ((cam.top - cam.bottom) / cam.zoom);
        const fc = await ctl.call("face_candidates", { camera: cam, parts: [part], edge_margin_px: 14, min_area_mm2: (32 / pxPerMm) ** 2, surface });
        tried.push({ az, el, n: fc.candidates[part].length, rejected: fc.stats[part].rejected });
        for (const c of fc.candidates[part]) {
            const prev = found.get(c.id);
            if (!prev || prev.edge_margin_px < c.edge_margin_px) found.set(c.id, { ...c, view });
        }
        if (found.size >= needed + 2) break;
    }
    return { part, zoom, candidates: [...found.values()].sort((a, b) => b.edge_margin_px - a.edge_margin_px), tried };
}

/** Click a candidate face with the properties tool active; the host resolves and checks it. */
async function pickFace(c) {
    const p = chrome.page;
    const cam = await setView(c.view);
    const [pp] = await pageProject([c.point]);
    const x = Math.round(pp[0]), y = Math.round(pp[1]);
    const top = await p.eval(`__s1.elementAt(${x}, ${y})`);
    const [py] = await ctl.call("project", { camera: cam, points: [c.point] });
    await ctl.call("expect_pick", { pick: { id: c.id, point: c.point } });
    const n0 = (await ctl.call("picks")).length;
    await canvasClick(x, y);
    let rec;
    try {
        rec = await waitUntil(async () => {
            const all = await ctl.call("picks");
            return all.length > n0 ? all[all.length - 1] : null;
        }, { timeoutMs: 60000, what: "pick record" });
    } catch (e) {
        rec = { error: String(e.message) };
    }
    await p.key("Escape", "Escape", 27);
    await waitFrames(1);
    return { expected: c.id, part: c.part, how: c.how, view: { az: c.view.az, el: c.view.el, zoom: c.view.zoom }, pagePx: pp, pythonPx: py, pageVsPythonPx: dist2(pp, py),
        clickPx: [x, y], topElementIsCanvas: top.isCanvas, edgeMarginPx: c.edge_margin_px, ...rec, ctx: undefined };
}

// ---------------------------------------------------------------------------------------------
// Q1: the stock UI inside our page
// ---------------------------------------------------------------------------------------------

async function q1() {
    const p = chrome.page;
    const r = {};
    const step = async (name, fn) => {
        try {
            r[name] = await fn();
        } catch (e) {
            r[name] = { pass: false, error: String(e.stack ?? e).slice(0, 2000) };
            await p.key("Escape", "Escape", 27).catch(() => {});
        }
        console.log(`   ${name}: ${r[name].pass}`);
        save();
    };
    r.versions = await p.eval("__s1.versions");
    r.gl = await p.eval("__s1.glInfo()");
    r.scene = await p.eval("__s1.sceneStats()");
    r.frame = await p.eval("__s1.frameInfo()");
    r.tree = { domRows: (await p.eval("__s1.treeLabels()")).length, leafGroups: r.scene.leaves, pass: r.scene.leaves === 58 };

    await step("toolbar", async () => {
        await clickButton("top");
        const top = await p.eval("__s1.cameraState()");
        const topDir = norm(sub(top.position, top.target));
        await clickButton("iso");
        const iso = await p.eval("__s1.cameraState()");
        const isoDir = norm(sub(iso.position, iso.target));
        const hostPos = await ctl.call("status_get", { key: "position" });
        return { topViewDir: topDir, isoViewDir: isoDir, hostSeesCamera: !!hostPos && Math.hypot(...sub(hostPos, iso.position)) < 1e-6,
            pass: topDir[2] > 0.999 && isoDir[2] < 0.9 };
    });

    await step("treeToggle", async () => {
        const label = "upper_deck";
        const path_ = `${ROOT}/${label}`;
        const ic = await p.eval(`__s1.treeIconRect(${JSON.stringify(label)}, 0)`);
        if (!ic) throw new Error("tree row not found");
        const px0 = (await p.eval("__s1.analyzePixels()")).nonBackground;
        await p.click(ic.x, ic.y, { settleMs: 30 });
        await sleep(300);
        await waitFrames(1);
        const stateHidden = await p.eval(`__s1.viewer.getState(${JSON.stringify(path_)})`);
        const px1 = (await p.eval("__s1.analyzePixels()")).nonBackground;
        const hostStates = await ctl.call("status_get", { key: "states" });
        await p.click(ic.x, ic.y, { settleMs: 30 });
        await sleep(300);
        await waitFrames(1);
        const stateShown = await p.eval(`__s1.viewer.getState(${JSON.stringify(path_)})`);
        return { label, stateHidden, stateShown, hostState: hostStates?.[path_] ?? null, pixelsBefore: px0, pixelsHidden: px1,
            pass: stateHidden?.[0] === 0 && stateShown?.[0] === 1 && hostStates?.[path_]?.[0] === 0 && px1 < px0 };
    });

    await step("clipping", async () => {
        const clipTab = await p.eval(`__s1.rectOf(".tcv_tab_clip")`);
        await p.click(clipTab.x, clipTab.y, { settleMs: 30 });
        await sleep(500);
        await waitFrames(1);
        const local = await p.eval("__s1.viewer.renderer.localClippingEnabled");
        const sl = await p.eval(`(() => { const e = document.querySelector('.tcv_sld_value_plane1'); const r = e.getBoundingClientRect(); return { x: r.left, y: r.top + r.height / 2, w: r.width, value: e.value, min: e.min, max: e.max }; })()`);
        const pxOpen = (await p.eval("__s1.analyzePixels()")).nonBackground;
        await p.click(sl.x + sl.w * 0.55, sl.y, { settleMs: 30 });
        await sleep(400);
        await waitFrames(1);
        const slider0 = await p.eval("__s1.viewer.getClipSlider(0)");
        const pxClipped = (await p.eval("__s1.analyzePixels()")).nonBackground;
        const hostSlider = await ctl.call("status_get", { key: "clip_slider_0" });
        await p.screenshot(path.join(RESULTS, `q1_clip_${BACKEND}.png`));
        const treeTab = await p.eval(`__s1.rectOf(".tcv_tab_tree")`);
        await p.click(treeTab.x, treeTab.y, { settleMs: 30 });
        await sleep(400);
        await waitFrames(1);
        const localAfter = await p.eval("__s1.viewer.renderer.localClippingEnabled");
        return { localClippingEnabled: local, localClippingAfterTreeTab: localAfter, sliderBefore: Number(sl.value), sliderAfter: slider0, hostSlider,
            pixelsOpen: pxOpen, pixelsClipped: pxClipped, pass: local === true && localAfter === false && Math.abs(slider0 - Number(sl.value)) > 1 && hostSlider === slider0 && pxClipped < pxOpen * 0.97 };
    });

    await step("measure", async () => {
        const fa = (await findCandidates("housing_lid", 1)).candidates[0];
        const fb = (await findCandidates("arm_platform", 1)).candidates[0];
        if (!fa || !fb) throw new Error("no faces for the measure test");
        // one view that shows both: the whole robot
        const view = viewAround(robotCentre, -60, 35, 2500, 1.0);
        await setView(view);
        const [pa, pb] = await pageProject([fa.point, fb.point]);
        await clickButton("properties");
        const toolHost = await ctl.call("status_get", { key: "activeTool" });
        await canvasClick(Math.round(pa[0]), Math.round(pa[1]));
        const propPanel = await waitUntil(() => p.eval(`(() => { const e = document.querySelector('.tcv_properties_measurement_panel'); return e?.style.display === 'inline-block' ? { display: e.style.display, text: e.innerText } : null; })()`),
            { timeoutMs: 60000, what: "properties panel" });
        const pickA = (await ctl.call("picks")).slice(-1)[0];
        const area = await ctl.call("face_area", { fid: pickA?.id ?? fa.id });
        await p.screenshot(path.join(RESULTS, `q1_properties_${BACKEND}.png`));
        await p.key("Escape", "Escape", 27);
        await waitFrames(1);
        await clickButton("properties"); // off
        await clickButton("distance");
        await canvasClick(Math.round(pa[0]), Math.round(pa[1]));
        await sleep(300);
        await canvasClick(Math.round(pb[0]), Math.round(pb[1]));
        const distPanel = await waitUntil(() => p.eval(`(() => { const e = document.querySelector('.tcv_distance_measurement_panel'); return e?.style.display === 'inline-block' ? { display: e.style.display, text: e.innerText } : null; })()`),
            { timeoutMs: 60000, what: "distance panel" });
        const sel = await ctl.call("status_get", { key: "selectedShapeIDs" });
        const [ida, idb] = Array.isArray(sel) ? sel.filter((s) => typeof s === "string") : [];
        const exactDist = ida && idb ? await ctl.call("distance", { a: ida, b: idb }) : null;
        await p.screenshot(path.join(RESULTS, `q1_distance_${BACKEND}.png`));
        await p.key("Escape", "Escape", 27);
        await waitFrames(1);
        await clickButton("distance"); // off
        const areaHit = numbersIn(propPanel.text).find((v) => Math.abs(v - area) <= Math.max(0.01, 1e-4 * area));
        const distHit = exactDist === null ? undefined : numbersIn(distPanel.text).find((v) => Math.abs(v - exactDist) <= 0.01);
        return { toolSeenByHost: toolHost, properties: { face: pickA?.id, panelText: propPanel.text.slice(0, 500), exactArea: area, panelShowsExactArea: areaHit !== undefined },
            distance: { faces: [ida, idb], panelText: distPanel.text.slice(0, 500), exactDistance: exactDist, panelShowsExactDistance: distHit !== undefined },
            pass: toolHost === "PropertiesMeasurement" && areaHit !== undefined && distHit !== undefined };
    });

    await step("explode", async () => {
        await setView(viewAround(robotCentre, -60, 35, 2500, 1.0));
        const probeParts = ["upper_deck", "column", "arm_platform", "front_left_wheel", "housing_lid"].map((l) => `${ROOT}/${l}`);
        const b0 = await p.eval(`__s1.partBoxes(${JSON.stringify(probeParts)})`);
        await clickButton("explode");
        const sl = await waitUntil(() => p.eval(`(() => { const e = Array.from(document.querySelectorAll('.tcv_animation_slider')).find((x) => x.getBoundingClientRect().width > 0); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.left, y: r.top + r.height / 2, w: r.width }; })()`),
            { timeoutMs: 10000, what: "explode slider" });
        await p.click(sl.x + sl.w * 0.98, sl.y, { settleMs: 30 });
        await sleep(400);
        await waitFrames(2);
        const b1 = await p.eval(`__s1.partBoxes(${JSON.stringify(probeParts)})`);
        const hostExplode = await ctl.call("status_get", { key: "explode" });
        await p.screenshot(path.join(RESULTS, `q1_explode_${BACKEND}.png`));
        await clickButton("explode");
        await sleep(400);
        await waitFrames(2);
        const b2 = await p.eval(`__s1.partBoxes(${JSON.stringify(probeParts)})`);
        const moved = probeParts.map((k) => Math.hypot(...sub(b1[k].min, b0[k].min)));
        const back = probeParts.map((k) => Math.hypot(...sub(b2[k].min, b0[k].min)));
        return { movedMm: moved, restoredErrorMm: back, hostSawExplode: hostExplode, pass: moved.filter((m) => m > 1).length >= 3 && Math.max(...back) < 1e-6 };
    });

    await step("getImage", async () => {
        const g = await p.eval("__s1.getImage()");
        return { ...g, pass: g.prefix === "data:image/png;base64," && g.length > 10000 };
    });
    r.console = p.console.slice(-20);
    r.pass = ["tree", "toolbar", "treeToggle", "clipping", "measure", "explode", "getImage"].every((k) => r[k]?.pass);
    return r;
}

// ---------------------------------------------------------------------------------------------
// Q2: 20 face picks over 5 parts, resolved and checked in Python
// ---------------------------------------------------------------------------------------------

const PICK_PARTS = ["camera", "hitch_receiver", "touch_display", "lift_carriage", "front_left_wheel_mount", "estop", "fan", "display", "master_switch",
    "upper_rail_block", "camera_mast", "usb_hub", "rear_wheel_mount", "column", "lidar", "housing_lid", "arm_platform"];

async function q2() {
    const search = [];
    const chosen = [];
    for (const part of PICK_PARTS) {
        if (chosen.length >= 20) break;
        const fc = await findCandidates(part, 4);
        search.push({ part, zoom: fc.zoom, found: fc.candidates.length, tried: fc.tried });
        save();
        if (fc.candidates.length >= 4) chosen.push(...fc.candidates.slice(0, 4));
    }
    if (chosen.length < 20) throw new Error(`only ${chosen.length / 4} parts with 4 pickable faces: ${JSON.stringify(search.map((s) => [s.part, s.found]))}`);
    await clickButton("properties");
    await ctl.call("clear_picks");
    const picks = [];
    for (const c of chosen) {
        picks.push(await pickFace(c));
        console.log(`   pick ${picks.length}: ${c.id} -> ${picks.at(-1).id} pass=${picks.at(-1).pass} err=${picks.at(-1).projection_error_px?.toFixed(3)}px`);
        out.sections.q2_picks = { partial: picks };
        save();
    }
    await clickButton("properties"); // off

    // part-level picks: double-click with no tool -> lastPick (Phase 1's "a pick returns lower_plate")
    const partPicks = [];
    for (const c of chosen.filter((_, i) => i % 4 === 0)) {
        await setView(c.view);
        const [pp] = await pageProject([c.point]);
        const x = Math.round(pp[0]), y = Math.round(pp[1]);
        const ev0 = (await ctl.call("events")).length;
        await canvasClick(x, y, { clickCount: 1 });
        await chrome.page.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 2 });
        await chrome.page.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 2 });
        const lp = await waitUntil(async () => (await ctl.call("events", { since: ev0 })).find((e) => e.event === "lastPick"), { timeoutMs: 30000, what: "lastPick" }).catch(() => null);
        partPicks.push({ expectedPart: c.part, got: lp ? `${lp.path}/${lp.name}` : null, pass: !!lp && lp.name === c.part && lp.path === ROOT });
        await chrome.page.key("Escape", "Escape", 27);
        await waitFrames(1);
    }
    const nPass = picks.filter((k) => k.pass).length;
    const errs = picks.map((k) => k.projection_error_px).filter((x) => Number.isFinite(x));
    return {
        search, parts: [...new Set(chosen.map((c) => c.part))], nPicks: picks.length, nPass,
        maxProjectionErrorPx: Math.max(...errs), meanProjectionErrorPx: errs.reduce((a, b) => a + b, 0) / errs.length,
        maxPageVsPythonPx: Math.max(...picks.map((k) => k.pageVsPythonPx)),
        maxPointToFaceMm: Math.max(...picks.map((k) => k.point_to_resolved_face_mm ?? Infinity)),
        maxAnchorToPointMm: Math.max(...picks.map((k) => k.anchor_to_point_mm ?? Infinity)),
        picks, partPicks, partPicksPass: partPicks.every((x) => x.pass),
        pass: picks.length === 20 && nPass === 20,
    };
}

/** Extra evidence, not a gate: picks on curved faces (the 20 of Q2 all landed on planes). */
async function q2curved() {
    const parts = ["lidar", "front_left_wheel", "front_right_wheel", "rear_wheel", "front_left_hub", "front_right_hub", "rear_hub", "estop_head", "estop",
        "master_switch", "capacitor", "front_left_servo", "rear_servo", "left_arm_base", "right_arm_base", "rail", "upper_rail_block", "front_left_wheel_mount"];
    const chosen = [];
    const search = [];
    for (const part of parts) {
        if (chosen.length >= 12) break;
        const fc = await findCandidates(part, 1, "curved");
        search.push({ part, found: fc.candidates.length, surfaces: fc.candidates.map((c) => c.surface) });
        chosen.push(...fc.candidates.slice(0, 1));
    }
    await clickButton("properties");
    await ctl.call("clear_picks");
    const picks = [];
    for (const c of chosen) {
        const k = await pickFace(c);
        picks.push({ ...k, surface: c.surface });
        console.log(`   curved pick ${picks.length}: ${c.id} (${c.surface}) -> ${k.id} pass=${k.pass} err=${k.projection_error_px?.toFixed(3)}px`);
    }
    await clickButton("properties");
    const nPass = picks.filter((k) => k.pass).length;
    return { search, nPicks: picks.length, nPass, surfaces: picks.map((k) => k.surface), maxProjectionErrorPx: Math.max(...picks.map((k) => k.projection_error_px ?? Infinity)),
        maxPointToFaceMm: Math.max(...picks.map((k) => k.point_to_resolved_face_mm ?? Infinity)), picks, pass: picks.length >= 8 && nPass === picks.length };
}

// ---------------------------------------------------------------------------------------------
// Q3: a pin and an arrow at a picked point, glued while orbiting, hidden behind geometry
// ---------------------------------------------------------------------------------------------

function pinSamples(A, n, L, R, arrowTail) {
    const H = add(A, mul(n, L + R));
    const pts = { head: [H], rim: [], needle: [], arrow: [] };
    for (const d of [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]) pts.rim.push(add(H, mul(d, R)));
    for (const s of [0.15, 0.4, 0.65, 0.9]) pts.needle.push(add(A, mul(n, L * s)));
    for (const s of [0.1, 0.35, 0.6, 0.85, 1.0]) pts.arrow.push(add(A, mul(sub(arrowTail, A), s)));
    return pts;
}

function pinItems(pick) {
    const A = pick.anchor;
    const n = norm(pick.anchor_normal);
    const t = norm(Math.abs(n[2]) < 0.9 ? cross(n, [0, 0, 1]) : cross(n, [1, 0, 0]));
    const L = 60, R = 9, AL = 90;
    const tail = add(A, mul(norm(add(n, t)), AL));
    const items = [
        { kind: "pin", id: "p1", point: A, normal: n, length: L, head: R },
        { kind: "arrow", id: "a1", tip: A, dir: norm(sub(A, tail)), length: AL, radius: 2.5 },
    ];
    return { A, n, L, R, AL, tail, items, S: pinSamples(A, n, L, R, tail) };
}

async function viewState(view) {
    return chrome.page.eval(`__s1.viewState(${JSON.stringify(view.position)}, ${JSON.stringify(view.target)}, ${view.zoom})`);
}

function onCanvas(cam, occ) {
    return occ.every((o) => o.px[0] > cam.canvas.x + 5 && o.px[0] < cam.canvas.x + cam.canvas.width - 5 && o.px[1] > cam.canvas.y + 5 && o.px[1] < cam.canvas.y + cam.canvas.height - 5);
}

const GLUE_VIEWS = [[-60, 35], [-10, 55], [40, 30], [90, 45], [-110, 40], [140, 50], [-160, 50], [0, 75], [-30, 15], [60, 15]];
const OCCL_VIEWS = [];
for (const el of [-30, -12, 8, 25, -50]) for (let az = -180; az < 180; az += 20) OCCL_VIEWS.push([az, el]);

/** Choose, among the resolved picks, an anchor with three glue views (whole head visible) and one view
 * where every sample of pin and arrow is behind geometry. Decided by exact ray casts in Python on
 * camera states computed in the page without rendering. */
async function chooseAnchor(picks) {
    const tried = [];
    for (const pick of picks) {
        const P = pinItems(pick);
        const all = [...P.S.head, ...P.S.rim, ...P.S.needle, ...P.S.arrow];
        const glue = [];
        for (const [az, el] of GLUE_VIEWS) {
            if (glue.length >= 3) break;
            const cam = await viewState(viewAround(robotCentre, az, el, 2500, 1.5));
            const occ = await ctl.call("occlusion", { camera: cam, points: [...P.S.head, ...P.S.rim, ...P.S.needle] });
            if (onCanvas(cam, occ) && !occ.some((o) => o.hidden)) glue.push([az, el]);
        }
        let occl = null;
        let best = 0;
        if (glue.length >= 3) {
            for (const [az, el] of OCCL_VIEWS) {
                const cam = await viewState(viewAround(robotCentre, az, el, 2500, 1.5));
                const occ = await ctl.call("occlusion", { camera: cam, points: all });
                const nHidden = occ.filter((o) => o.hidden).length;
                best = Math.max(best, nHidden);
                if (onCanvas(cam, occ) && nHidden === occ.length) {
                    occl = { az, el, occluders: [...new Set(occ.map((o) => (o.hit_face ?? "").split("/faces/")[0]))] };
                    break;
                }
            }
        }
        tried.push({ anchor: pick.expected, glueViews: glue.length, occlusionView: occl, mostHidden: best, samples: all.length });
        if (glue.length >= 3 && occl) return { pick, P, glue, occl, tried };
    }
    return { tried };
}

async function q3(q2res) {
    const p = chrome.page;
    const usable = (q2res?.picks ?? []).filter((k) => k.pass && k.anchor && k.anchor_normal);
    if (!usable.length) throw new Error("no resolved pick with an exact anchor");
    usable.sort((a, b) => a.anchor[2] - b.anchor[2]); // low anchors first: more geometry around them
    const choice = await chooseAnchor(usable);
    if (!choice.pick) return { anchorSearch: choice.tried, pass: false, error: "no anchor with three glue views and a fully occluding view" };
    const { pick, P, occl } = choice;
    await ctl.call("send_overlay", { items: P.items });
    await waitFrames(1);

    // glue: orbit around the robot's centre (not the pin), the head's pixel centroid against an
    // independent projection (Python, from the camera state the page reports)
    const glue = [];
    for (const [az, el] of choice.glue) {
        const cam = await setView(viewAround(robotCentre, az, el, 2500, 1.5));
        const px = await p.eval("__s1.analyzePixels()");
        const [py] = await ctl.call("project", { camera: cam, points: P.S.head });
        const [pg] = await pageProject(P.S.head);
        const shot = await p.screenshot(path.join(RESULTS, `q3_glue_${glue.length + 1}_${BACKEND}.png`));
        const c = px.colors.pinHead;
        const rPx = P.R * (cam.canvas.height / ((cam.top - cam.bottom) / cam.zoom));
        const err = c.n ? dist2(c.centroidPage, py) : null;
        glue.push({ az, el, zoom: 1.5, headPixels: c.n, expectedDiscPx: Math.PI * rPx * rPx, centroid: c.centroidPage ?? null, pythonProjected: py.slice(0, 2),
            pageProjected: pg.slice(0, 2), errorPx: err, needlePixels: px.colors.pinNeedle.n, arrowPixels: px.colors.arrow.n, screenshot: path.basename(shot),
            pass: c.n > 0.6 * Math.PI * rPx * rPx && err !== null && err <= 2.0 });
    }

    // occlusion: every sample of pin and arrow behind geometry -> no overlay pixel may show; control
    // with depth testing off shows the pin exactly where it would be
    const cam = await setView(viewAround(robotCentre, occl.az, occl.el, 2500, 1.5));
    const hidden = await p.eval("__s1.analyzePixels()");
    await p.screenshot(path.join(RESULTS, `q3_occluded_${BACKEND}.png`));
    await p.eval("__s1.overlay.setDepthTest(false)");
    const control = await p.eval("__s1.analyzePixels()");
    await p.screenshot(path.join(RESULTS, `q3_occluded_control_nodepth_${BACKEND}.png`));
    await p.eval("__s1.overlay.setDepthTest(true)");
    await waitFrames(1);
    const [py] = await ctl.call("project", { camera: cam, points: P.S.head });
    const occlusion = { ...occl,
        withDepthTest: { head: hidden.colors.pinHead.n, needle: hidden.colors.pinNeedle.n, arrow: hidden.colors.arrow.n },
        controlNoDepthTest: { head: control.colors.pinHead.n, needle: control.colors.pinNeedle.n, arrow: control.colors.arrow.n,
            headCentroidErrorPx: control.colors.pinHead.n ? dist2(control.colors.pinHead.centroidPage, py) : null } };
    occlusion.pass = occlusion.withDepthTest.head === 0 && occlusion.withDepthTest.needle === 0 && occlusion.withDepthTest.arrow === 0
        && occlusion.controlNoDepthTest.head > 20 && occlusion.controlNoDepthTest.headCentroidErrorPx <= 2.0;

    // a face pick through the pin still reaches the CAD face (the overlay is not in the pick buffer)
    const overlayStats = await p.eval("({ draws: __s1.overlay.draws, skippedTargets: __s1.overlay.skippedTargets, lastDrawMs: __s1.overlay.lastDrawMs })");
    return { anchorFrom: pick.expected, anchor: P.A, normal: P.n, pin: { length: P.L, head: P.R }, arrow: { length: P.AL }, anchorSearch: choice.tried,
        glue, occlusion, overlayStats, pass: glue.length === 3 && glue.every((g) => g.pass) && occlusion.pass };
}

// ---------------------------------------------------------------------------------------------
// Q5: incremental updates
// ---------------------------------------------------------------------------------------------

function boxError(boxes, exact, labels) {
    return Math.max(...labels.map((l) => {
        const k = `${ROOT}/${l}`;
        const e = exact[l];
        return Math.max(...[0, 1, 2].map((i) => Math.abs(boxes[k].min[i] - e[i])), ...[0, 1, 2].map((i) => Math.abs(boxes[k].max[i] - e[i + 3])));
    }));
}

async function pickOn(parts) {
    for (const part of [].concat(parts)) {
        const fc = await findCandidates(part, 1);
        const c = fc.candidates[0];
        if (!c) continue;
        await clickButton("properties");
        const r = await pickFace(c);
        await clickButton("properties");
        return r;
    }
    return { error: `no candidate on ${parts}` };
}

async function q5() {
    const p = chrome.page;
    const r = {};
    const step = async (name, fn) => {
        try {
            r[name] = await fn();
        } catch (e) {
            r[name] = { pass: false, error: String(e.stack ?? e).slice(0, 2000) };
        }
        console.log(`   ${name}: ${r[name].pass}`);
        save();
    };
    const H2 = 500.0;
    await step("liftTransform", async () => {
        const ev0 = (await ctl.call("events")).length;
        const upd = await ctl.call("diff_update", { height: H2, mode: "transform" });
        const ev = await waitUntil(async () => (await ctl.call("events", { since: ev0 })).find((e) => e.event === "set-locs"), { what: "set-locs", timeoutMs: 60000 });
        const boxes = await p.eval(`__s1.partBoxes(${JSON.stringify(upd.moved.map((l) => `${ROOT}/${l}`))})`);
        const exact = await ctl.call("part_world_bbox", { labels: upd.moved });
        const pick = await pickOn(["camera", "lift_carriage", "camera_mast", "arm_platform"]);
        r._movedBoxes = boxes;
        r._moved = upd.moved;
        const err = boxError(boxes, exact, upd.moved);
        return { height: H2, moved: upd.moved, regeom: upd.regeom, bytesSent: upd.bytes_sent, hostBuildS: upd.build_s, hostDiffS: upd.diff_s,
            hostBackendUpdateS: upd.backend_update_s, pageApplyMs: ev.applyMs, pageGpuDoneMs: ev.gpuDoneMs, latencyFromSendMs: ev.latencyFromSendMs,
            maxBoxDiffVsExactMm: err, pickAfterMove: pick, pass: upd.regeom.length === 0 && upd.moved.length > 0 && err < 0.2 && !!pick?.pass };
    });
    await step("fullReshow", async () => {
        const full = await showAndWait({ height: H2, reset_camera: "keep" });
        const moved = r._moved ?? [];
        const boxes = await p.eval(`__s1.partBoxes(${JSON.stringify(moved.map((l) => `${ROOT}/${l}`))})`);
        const diff = moved.length ? Math.max(...moved.map((l) => {
            const k = `${ROOT}/${l}`;
            return Math.max(...sub(boxes[k].min, r._movedBoxes[k].min).map(Math.abs), ...sub(boxes[k].max, r._movedBoxes[k].max).map(Math.abs));
        })) : null;
        return { height: H2, payloadBytes: full.host.payload_bytes, tessellateConvertS: full.host.tessellate_convert_s, showTotalS: full.host.show_total_s,
            pageHandleMs: full.page.handleMs, gpuDoneMs: full.page.gpuDoneMs, showToFirstFrameMs: full.showToFirstFrameMs,
            maxBoxDiffIncrementalVsFullMm: diff, pass: diff !== null && diff < 1e-6 };
    });
    await step("liftReadd", async () => {
        const ev0 = (await ctl.call("events")).length;
        const upd = await ctl.call("diff_update", { height: null, mode: "readd" });
        const ev = await waitUntil(async () => (await ctl.call("events", { since: ev0 })).find((e) => e.event === "set-locs"), { what: "set-locs readd", timeoutMs: 60000 });
        const boxes = await p.eval(`__s1.partBoxes(${JSON.stringify(upd.moved.map((l) => `${ROOT}/${l}`))})`);
        const exact = await ctl.call("part_world_bbox", { labels: upd.moved });
        const leaves = (await p.eval("__s1.sceneStats()")).leaves;
        const pick = await pickOn(["lift_carriage", "camera", "camera_mast"]);
        const err = boxError(boxes, exact, upd.moved);
        return { moved: upd.moved, regeom: upd.regeom, bytesSent: upd.bytes_sent, pageApplyMs: ev.applyMs, pageGpuDoneMs: ev.gpuDoneMs, leavesAfter: leaves,
            maxBoxDiffVsExactMm: err, pickAfterReadd: pick, pass: upd.regeom.length === 0 && leaves === 58 && err < 0.2 && !!pick?.pass };
    });
    await step("geometryUpdate", async () => {
        const ev0 = (await ctl.call("events")).length;
        const change = { edge_margin: 16.0 };
        const upd = await ctl.call("diff_update", { platform: change, mode: "transform" });
        const ev = await waitUntil(async () => (await ctl.call("events", { since: ev0 })).find((e) => e.event === "update-parts"), { what: "update-parts", timeoutMs: 60000 });
        const boxes = await p.eval(`__s1.partBoxes(${JSON.stringify(upd.regeom.map((l) => `${ROOT}/${l}`))})`);
        const exact = await ctl.call("part_world_bbox", { labels: upd.regeom });
        const leaves = (await p.eval("__s1.sceneStats()")).leaves;
        const pick = await pickOn(upd.regeom);
        const err = boxError(boxes, exact, upd.regeom);
        return { change, regeom: upd.regeom, moved: upd.moved, bytesSent: upd.bytes_sent, hostBuildS: upd.build_s, hostTessellateS: upd.tessellate_s,
            hostEncodeS: upd.encode_s, hostBackendUpdateS: upd.backend_update_s, pageDecodeApplyMs: ev.decodeApplyMs, pageGpuDoneMs: ev.gpuDoneMs,
            latencyFromSendMs: ev.latencyFromSendMs, leavesAfter: leaves, maxBoxDiffVsExactMm: err, pickAfterGeom: pick,
            pass: upd.regeom.length >= 1 && upd.regeom.length <= 3 && leaves === 58 && err < 0.2 && !!pick?.pass };
    });
    delete r._movedBoxes;
    delete r._moved;
    r.pass = ["liftTransform", "fullReshow", "liftReadd", "geometryUpdate"].every((k) => r[k]?.pass);
    return r;
}

// ---------------------------------------------------------------------------------------------
// Q4: performance at each size, capped (default vsync) and uncapped
// ---------------------------------------------------------------------------------------------

function pickHost(h) {
    return { build_s: h.build_s, tessellate_convert_s: h.tessellate_convert_s, encode_s: h.encode_s, payload_bytes: h.payload_bytes, ws_send_s: h.ws_send_s,
        backend_encode_s: h.backend_encode_s, backend_payload_bytes: h.backend_payload_bytes, backend_load_s: h.backend_load_s, show_total_s: h.show_total_s };
}

async function perfAt(width, height, uncapped) {
    await launch(width, height, uncapped);
    const p = chrome.page;
    const cold = await showAndWait({}); // new page: shader compile, first upload
    await sceneGeometry();
    await sleep(300);
    const warm = await showAndWait({ reset_camera: "keep" }); // same page, cached tessellation
    await setView(viewAround(robotCentre, -60, 35, 2500, 1.0));
    const gl = await p.eval("__s1.glInfo()");
    const scene = await p.eval("__s1.sceneStats()");
    const frame = await p.eval("__s1.frameInfo()");
    const raf = await p.eval(`__s1.bench({ frames: ${FRAMES}, degPerFrame: ${360 / FRAMES}, sync: false })`, { timeoutMs: 1800000 });
    const syncd = await p.eval(`__s1.bench({ frames: ${SYNC_FRAMES}, degPerFrame: ${360 / SYNC_FRAMES}, sync: true })`, { timeoutMs: 1800000 });
    const timer = gl.timerQuery ? await p.eval(`__s1.bench({ frames: ${SYNC_FRAMES}, degPerFrame: ${360 / SYNC_FRAMES}, sync: false, gpuTimer: true })`, { timeoutMs: 1800000 }) : null;
    return {
        window: [width, height], uncapped, canvas: raf.canvas, gl: { renderer: gl.renderer, version: gl.version, maxSamples: gl.maxSamples, timerQuery: gl.timerQuery },
        scene, frame,
        show: {
            cold: { showToFirstFrameMs: cold.showToFirstFrameMs, sendToRecvMs: cold.sendToRecvMs, host: pickHost(cold.host), page: cold.page },
            warm: { showToFirstFrameMs: warm.showToFirstFrameMs, sendToRecvMs: warm.sendToRecvMs, host: pickHost(warm.host), page: warm.page },
        },
        orbitRaf: raf, orbitSync: syncd, orbitGpuTimer: timer,
    };
}

// ---------------------------------------------------------------------------------------------

try {
    await startHost(PORT, hostLog);
    ctl = await controlClient(PORT);
    out.ping = await ctl.call("ping");
    if (SUITE === "full" || SUITE === "func") {
        await launch(1600, 1000, false);
        await section("first_show", async () => showAndWait({}));
        await sceneGeometry();
        await section("q1_stock_ui", q1);
        const r2 = await section("q2_picks", q2);
        await section("q3_overlay", () => q3(r2));
        await chrome.page.eval("__s1.overlay.setItems([])");
        await section("q5_incremental", q5);
        out.chromeConsole = chrome.page.console.slice(-30);
    }
    if (SUITE === "curved") {
        await launch(1600, 1000, false);
        await section("first_show", async () => showAndWait({}));
        await sceneGeometry();
        await section("q2_curved_picks", q2curved);
    }
    if (SUITE === "full" || SUITE === "perf") {
        for (const [w, h] of SIZES) {
            for (const uncapped of [false, true]) {
                await section(`q4_perf_${w}x${h}_${uncapped ? "uncapped" : "capped"}`, () => perfAt(w, h, uncapped));
            }
        }
    }
    out.hostState = await ctl.call("state");
    await ctl.call("shutdown").catch(() => {});
} catch (e) {
    out.fatal = String(e.stack ?? e);
    console.error("FATAL", e);
} finally {
    out.hostLogTail = hostLog.slice(-40);
    out.chromeLogTail = (chrome?.log ?? []).join("").split("\n").filter((l) => /ERROR|FATAL|WARN/i.test(l)).slice(-20);
    out.finished = new Date().toISOString();
    save();
    await stopAll();
    console.log("wrote", path.join(RESULTS, OUTFILE));
}
