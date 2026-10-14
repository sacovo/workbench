// Spike S1 page: the stock viewer (three-cad-viewer through ocp-viewer-core's createPage)
// mounted in our own page, with our own transport to the Python host, our own overlay drawn
// in the viewer's GL context, and test hooks on window.__s1 for the CDP driver.

import { Viewer, Display, Timer, decodeInstancedFormat, version as tcvVersion } from "three-cad-viewer";
import "three-cad-viewer/css";
import { createPage, VERSION as coreVersion, type Page } from "ocp-viewer-core";
import type * as THREE_NS from "three";
import { COLORS, Overlay, type OverlayItem } from "./overlay";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const overlay = new Overlay();
let viewer: HostViewer | null = null;
let page: Page | null = null;
let ws: WebSocket;

const S1: Any = ((window as Any).__s1 = {
    versions: { threeCadViewer: tcvVersion, ocpViewerCore: coreVersion },
    timings: [] as Any[],
    events: [] as Any[],
    incoming: [] as Any[],
    outgoingPicks: [] as Any[],
    frames: 0,
    sent: { status: 0, log: 0, other: 0, pickForwards: 0 },
    overlay,
    get viewer() {
        return viewer;
    },
});

function T(): typeof THREE_NS {
    return (window as Any).THREE;
}

// --------------------------------------------------------------------------------------
// The one subclass: capture the instance createPage builds (it does not return it), and
// re-attach the overlay to the new scene after every render.
// --------------------------------------------------------------------------------------

class HostViewer extends Viewer {
    constructor(display: Any, options: Any, notifyCallback: Any, pinAsPngCallback?: Any, updateMarker?: boolean) {
        super(display, options, notifyCallback, pinAsPngCallback ?? null, updateMarker ?? true);
        viewer = this;
        // public hook, called at the end of every update(): counts frames for the driver
        this.onAfterRender = () => {
            S1.frames++;
        };
        S1.events.push({ t: performance.now(), event: "viewer-created" });
    }

    render(shapes: Any, renderOptions: Any, viewerOptions: Any) {
        super.render(shapes, renderOptions, viewerOptions);
        overlay.attach(this.scene); // the scene is new after every render
    }
}

// --------------------------------------------------------------------------------------
// Transport: one websocket to the host. Outgoing = createPage's send(command, message);
// incoming = models/configs for createPage, plus our own "wb_*" messages.
// --------------------------------------------------------------------------------------

let lastPointer: { x: number; y: number; t: number } | null = null;
// Capture phase on the document: runs before the viewer's own mouseup handler commits a selection.
document.addEventListener(
    "pointerup",
    (e) => {
        lastPointer = { x: e.clientX, y: e.clientY, t: performance.now() };
    },
    true,
);

function wsSend(obj: unknown) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function send(command: string, message: Any) {
    const msg: Any = { kind: command, text: message };
    if (command === "status" && message && ("selectedShapeIDs" in message || "lastPick" in message)) {
        // A pick: forward the pointer and the camera with it, so the kernel can turn the
        // click into an exact ray.
        msg.ctx = { pointer: lastPointer, camera: cameraState() };
        S1.sent.pickForwards++;
        S1.outgoingPicks.push({ t: performance.now(), selectedShapeIDs: message.selectedShapeIDs, lastPick: message.lastPick ? `${message.lastPick.path}/${message.lastPick.name}` : undefined });
    }
    S1.sent[command === "status" ? "status" : command === "log" ? "log" : "other"]++;
    wsSend(msg);
}

function boot(settings: Any) {
    page = createPage({
        Viewer: HostViewer,
        Display,
        Timer,
        send,
        theme: settings.theme,
        overrides: {
            display: { treeWidth: settings.tree_width, glass: settings.glass, tools: settings.tools, theme: settings.theme, zscaleTool: false },
            viewer: { tools: settings.tools, glass: settings.glass, up: settings.up, control: settings.control },
        },
    });
    page.showSplash({ theme: settings.theme, treeWidth: settings.tree_width, keymap: settings.modifier_keys });
    S1.booted = true;
}

function connect() {
    ws = new WebSocket(`ws://${location.host}/`);
    ws.onopen = () => wsSend({ role: "page" });
    ws.onmessage = (ev) => {
        const recv = performance.now();
        const text = ev.data as string;
        const msg = JSON.parse(text);
        const parsed = performance.now();
        if (msg.type === "wb_hello") {
            boot(msg.settings);
            return;
        }
        if (typeof msg.type === "string" && msg.type.startsWith("wb_")) {
            handleOwn(msg, recv);
            return;
        }
        const isModel = msg.type === "data";
        S1.incoming.push({ t: recv, type: msg.type, tool: msg.tool_type, chars: text.length });
        page!.handleMessage(msg);
        const handled = performance.now();
        if (isModel) recordModelTiming(text.length, recv, parsed, handled);
    };
    ws.onclose = () => S1.events.push({ t: performance.now(), event: "ws-closed" });
}

function recordModelTiming(chars: number, recv: number, parsed: number, handled: number) {
    const rec: Any = {
        chars,
        recvWallMs: performance.timeOrigin + recv,
        parseMs: parsed - recv,
        handleMs: handled - parsed, // decode + build scene + tree + first update() (GL calls issued)
    };
    const gl = viewer!.renderer.getContext();
    const px = new Uint8Array(4);
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); // waits for the GPU to finish the frame
    rec.gpuDoneMs = performance.now() - handled;
    requestAnimationFrame(() => {
        const raf = performance.now();
        rec.firstRafAfterMs = raf - handled;
        rec.firstFrameWallMs = performance.timeOrigin + raf;
        rec.info = rendererInfo();
        S1.timings.push(rec);
        wsSend({ kind: "wb_event", event: { event: "model-shown", ...rec } });
    });
}

// --------------------------------------------------------------------------------------
// Our own messages
// --------------------------------------------------------------------------------------

function handleOwn(msg: Any, recv: number) {
    const v = viewer!;
    if (msg.type === "wb_overlay") {
        overlay.setItems(msg.items as OverlayItem[]);
        v.update(true, false);
        S1.events.push({ t: recv, event: "overlay", n: msg.items.length });
    } else if (msg.type === "wb_set_locs") {
        const t0 = performance.now();
        const paths = Object.keys(msg.locs);
        if (msg.mode === "transform") {
            // Move the existing groups: no geometry is touched. Uses the group map (not @public).
            const groups = (v.nestedGroup as Any).groups as Record<string, THREE_NS.Object3D>;
            for (const path of paths) {
                const [t, q] = msg.locs[path];
                const g = groups[path];
                g.position.set(t[0], t[1], t[2]);
                g.quaternion.set(q[0], q[1], q[2], q[3]);
            }
            v.updateBounds(); // public: bounds, pick buffer, highlight texture, re-render
        } else {
            // Public API only: remove and re-add each part with the geometry the page already has.
            for (const path of paths) {
                const entry = findShapeEntry(v.shapes, path);
                const parent = path.slice(0, path.lastIndexOf("/"));
                const data = { ...entry, loc: msg.locs[path] };
                v.removePart(path, { skipBounds: true });
                v.addPart(parent, data, { skipBounds: true });
            }
            v.updateBounds();
        }
        const t1 = performance.now();
        const gl = v.renderer.getContext();
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
        const rec = { event: "set-locs", mode: msg.mode, n: paths.length, applyMs: t1 - t0, gpuDoneMs: performance.now() - t1,
            latencyFromSendMs: performance.timeOrigin + performance.now() - msg.sent_wall_ms };
        S1.events.push(rec);
        wsSend({ kind: "wb_event", event: rec });
    } else if (msg.type === "wb_update_parts") {
        const t0 = performance.now();
        const paths = Object.keys(msg.parts);
        for (const path of paths) {
            const tree = decodeInstancedFormat(msg.parts[path]) as Any; // public export
            const leaf = tree.parts[0];
            leaf.name = path.slice(path.lastIndexOf("/") + 1);
            leaf.id = path;
            v.updatePart(path, leaf, { skipBounds: true }); // public
        }
        v.updateBounds();
        const t1 = performance.now();
        const gl = v.renderer.getContext();
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
        const rec = { event: "update-parts", n: paths.length, decodeApplyMs: t1 - t0, gpuDoneMs: performance.now() - t1,
            latencyFromSendMs: performance.timeOrigin + performance.now() - msg.sent_wall_ms };
        S1.events.push(rec);
        wsSend({ kind: "wb_event", event: rec });
    }
}

function findShapeEntry(node: Any, path: string): Any {
    if (!node) return null;
    if (node.id === path) return node;
    for (const p of node.parts ?? []) {
        const hit = findShapeEntry(p, path);
        if (hit) return hit;
    }
    return null;
}

// --------------------------------------------------------------------------------------
// Test hooks for the CDP driver
// --------------------------------------------------------------------------------------

function cameraState() {
    const v = viewer!;
    const cam = v.camera.getCamera() as Any; // Camera wrapper is a TS-public getter; frustum lives on the THREE camera
    const rect = v.renderer.domElement.getBoundingClientRect();
    return {
        position: v.getCameraPosition(),
        quaternion: v.getCameraQuaternion(),
        target: v.getCameraTarget(),
        zoom: v.getCameraZoom(),
        ortho: v.getOrtho(),
        left: cam.left, right: cam.right, top: cam.top, bottom: cam.bottom,
        near: cam.near, far: cam.far, fov: cam.fov, aspect: cam.aspect,
        canvas: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
        dpr: window.devicePixelRatio,
        drawingBuffer: [v.renderer.domElement.width, v.renderer.domElement.height],
    };
}

function project(points: [number, number, number][]) {
    const v = viewer!;
    const cam = v.camera.getCamera();
    cam.updateMatrixWorld();
    const rect = v.renderer.domElement.getBoundingClientRect();
    return points.map((p) => {
        const q = new (T().Vector3)(p[0], p[1], p[2]).project(cam);
        return [rect.left + ((q.x + 1) / 2) * rect.width, rect.top + ((1 - q.y) / 2) * rect.height, q.z];
    });
}

/** Look at `target` from `position` (Z up) through the viewer's public camera API. */
function setView(position: number[], target: number[], zoom: number | null = null) {
    const d = new (T().OrthographicCamera)();
    d.up.set(0, 0, 1);
    d.position.set(position[0], position[1], position[2]);
    d.lookAt(target[0], target[1], target[2]);
    viewer!.setCameraLocationSettings(position as Any, d.quaternion.toArray() as Any, target as Any, zoom, true);
    return cameraState();
}

/** The camera state setView(position, target, zoom) would produce, computed without rendering. */
function viewState(position: number[], target: number[], zoom: number) {
    const d = new (T().OrthographicCamera)();
    d.up.set(0, 0, 1);
    d.position.set(position[0], position[1], position[2]);
    d.lookAt(target[0], target[1], target[2]);
    return { ...cameraState(), position, quaternion: d.quaternion.toArray(), target, zoom };
}

/** Camera on a sphere around the current target: azimuth from +X, elevation from the XY plane. */
function orbitTo(azDeg: number, elDeg: number, zoom: number | null = null) {
    const v = viewer!;
    const target = v.getCameraTarget();
    const p = v.getCameraPosition();
    const r = Math.hypot(p[0] - target[0], p[1] - target[1], p[2] - target[2]);
    const az = (azDeg * Math.PI) / 180;
    const el = (elDeg * Math.PI) / 180;
    const pos = [target[0] + r * Math.cos(el) * Math.cos(az), target[1] + r * Math.cos(el) * Math.sin(az), target[2] + r * Math.sin(el)];
    return setView(pos, target, zoom);
}

function rendererInfo() {
    const r = viewer!.renderer;
    return { calls: r.info.render.calls, triangles: r.info.render.triangles, lines: r.info.render.lines, points: r.info.render.points,
        geometries: r.info.memory.geometries, textures: r.info.memory.textures, programs: r.info.programs?.length ?? null };
}

function glInfo() {
    const gl = viewer!.renderer.getContext() as WebGL2RenderingContext;
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    return {
        version: gl.getParameter(gl.VERSION),
        vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
        renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
        attributes: gl.getContextAttributes(),
        timerQuery: !!gl.getExtension("EXT_disjoint_timer_query_webgl2"),
        colorBufferFloat: !!gl.getExtension("EXT_color_buffer_float"),
        maxSamples: gl.getParameter(gl.MAX_SAMPLES),
    };
}

/** Geometry actually on the GPU for the model (not the per-frame counters). */
function sceneStats() {
    const v = viewer!;
    let meshes = 0, meshTris = 0, uniqueTris = 0, lineSegs = 0, points = 0;
    const seen = new Set<string>();
    v.scene.traverse((o: Any) => {
        if (o.isMesh && o.geometry) {
            meshes++;
            const g = o.geometry;
            const n = g.index ? g.index.count / 3 : (g.attributes.position?.count ?? 0) / 3;
            meshTris += n; // per mesh object, counting shared geometry (front/back/stencil) every time
            if (!seen.has(g.uuid)) {
                seen.add(g.uuid);
                uniqueTris += n;
            }
        } else if (o.isLineSegments2 || o.isLineSegments) {
            const g = o.geometry;
            if (!seen.has(g.uuid)) {
                seen.add(g.uuid);
                lineSegs += g.attributes.instanceStart ? g.attributes.instanceStart.count : (g.attributes.position?.count ?? 0) / 2;
            }
        } else if (o.isPoints) {
            if (!seen.has(o.geometry.uuid)) {
                seen.add(o.geometry.uuid);
                points += o.geometry.attributes.position?.count ?? 0;
            }
        }
    });
    const groups = (v.nestedGroup as Any).groups as Record<string, Any>;
    const leaves = Object.values(groups).filter((g: Any) => !!g.shapeGeometry).length;
    return { meshes, meshTriangles: meshTris, uniqueTriangles: uniqueTris, uniqueLineSegments: lineSegs, uniquePoints: points, leaves };
}

/** What one ordinary frame draws (all render calls of one update(), summed). */
function frameInfo() {
    const r = viewer!.renderer;
    r.info.autoReset = false;
    r.info.reset();
    viewer!.update(true, false);
    const out = { calls: r.info.render.calls, triangles: r.info.render.triangles, lines: r.info.render.lines, points: r.info.render.points };
    r.info.autoReset = true;
    return out;
}

/** Render now and count overlay-coloured pixels straight from the GL drawing buffer. */
function analyzePixels(tol = 10) {
    const v = viewer!;
    v.update(true, false);
    const gl = v.renderer.getContext();
    const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
    const buf = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
    const want: Record<string, [number, number, number]> = {
        pinHead: [(COLORS.pinHead >> 16) & 255, (COLORS.pinHead >> 8) & 255, COLORS.pinHead & 255],
        pinNeedle: [(COLORS.pinNeedle >> 16) & 255, (COLORS.pinNeedle >> 8) & 255, COLORS.pinNeedle & 255],
        arrow: [(COLORS.arrow >> 16) & 255, (COLORS.arrow >> 8) & 255, COLORS.arrow & 255],
    };
    const acc: Record<string, { n: number; sx: number; sy: number; minx: number; maxx: number; miny: number; maxy: number }> = {};
    for (const k of Object.keys(want)) acc[k] = { n: 0, sx: 0, sy: 0, minx: 1e9, maxx: -1, miny: 1e9, maxy: -1 };
    let nonBackground = 0;
    for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
            const o = (y * w + x) * 4;
            const r = buf[o], g = buf[o + 1], b = buf[o + 2], a = buf[o + 3];
            if (a > 0) nonBackground++;
            for (const k in want) {
                const c = want[k];
                if (Math.abs(r - c[0]) <= tol && Math.abs(g - c[1]) <= tol && Math.abs(b - c[2]) <= tol && a > 200) {
                    const s = acc[k];
                    const yy = h - 1 - y; // GL rows are bottom-up
                    s.n++; s.sx += x + 0.5; s.sy += yy + 0.5;
                    s.minx = Math.min(s.minx, x); s.maxx = Math.max(s.maxx, x); s.miny = Math.min(s.miny, yy); s.maxy = Math.max(s.maxy, yy);
                }
            }
        }
    }
    const rect = v.renderer.domElement.getBoundingClientRect();
    const scale = rect.width / w;
    const out: Any = { width: w, height: h, nonBackground, colors: {} };
    for (const k in acc) {
        const s = acc[k];
        out.colors[k] = s.n === 0 ? { n: 0 } : {
            n: s.n,
            centroidPage: [rect.left + (s.sx / s.n) * scale, rect.top + (s.sy / s.n) * scale],
            bboxPage: [rect.left + s.minx * scale, rect.top + s.miny * scale, rect.left + (s.maxx + 1) * scale, rect.top + (s.maxy + 1) * scale],
        };
    }
    return out;
}

/** Scripted orbit around the target, one camera step per animation frame, timed in the page. */
async function bench(opts: { frames?: number; degPerFrame?: number; sync?: boolean; notify?: boolean; gpuTimer?: boolean } = {}) {
    const frames = opts.frames ?? 300;
    const deg = opts.degPerFrame ?? 1.2;
    const sync = opts.sync ?? false;
    const notify = opts.notify ?? true;
    const v = viewer!;
    const gl = v.renderer.getContext() as WebGL2RenderingContext;
    const target = v.getCameraTarget();
    const p0 = v.getCameraPosition();
    const rel = [p0[0] - target[0], p0[1] - target[1], p0[2] - target[2]];
    const rXY = Math.hypot(rel[0], rel[1]);
    const a0 = Math.atan2(rel[1], rel[0]);
    const dummy = new (T().OrthographicCamera)();
    dummy.up.set(0, 0, 1);
    const tq = opts.gpuTimer ? (gl.getExtension("EXT_disjoint_timer_query_webgl2") as Any) : null;
    const queries: WebGLQuery[] = [];
    const intervals: number[] = [];
    const cpu: number[] = [];
    const px = new Uint8Array(4);
    const sentBefore = S1.sent.status;
    let prev: number | null = null;
    const t0 = performance.now();
    await new Promise<void>((resolve) => {
        let i = 0;
        const step = (now: number) => {
            if (prev !== null) intervals.push(now - prev);
            prev = now;
            const a = a0 + ((i * deg) * Math.PI) / 180;
            const pos = [target[0] + rXY * Math.cos(a), target[1] + rXY * Math.sin(a), target[2] + rel[2]];
            dummy.position.set(pos[0], pos[1], pos[2]);
            dummy.lookAt(target[0], target[1], target[2]);
            const c0 = performance.now();
            let q: WebGLQuery | null = null;
            if (tq) {
                q = gl.createQuery()!;
                gl.beginQuery(tq.TIME_ELAPSED_EXT, q);
            }
            // the same call path a trackball drag takes: camera change -> controls.update -> update(true, notify)
            v.setCameraLocationSettings(pos as Any, dummy.quaternion.toArray() as Any, null, null, notify);
            if (tq && q) {
                gl.endQuery(tq.TIME_ELAPSED_EXT);
                queries.push(q);
            }
            if (sync) gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
            cpu.push(performance.now() - c0);
            i++;
            if (i <= frames) requestAnimationFrame(step);
            else resolve();
        };
        requestAnimationFrame(step);
    });
    const wall = performance.now() - t0;
    let gpu: number[] = [];
    if (tq) {
        await new Promise((r) => setTimeout(r, 300));
        gpu = queries.map((q) => {
            const avail = gl.getQueryParameter(q, gl.QUERY_RESULT_AVAILABLE);
            const disjoint = gl.getParameter(tq.GPU_DISJOINT_EXT);
            return avail && !disjoint ? gl.getQueryParameter(q, gl.QUERY_RESULT) / 1e6 : NaN;
        });
    }
    const st = (a: number[]) => {
        const s = [...a].filter((x) => Number.isFinite(x)).sort((x, y) => x - y);
        if (s.length === 0) return null;
        const q = (p: number) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
        return { n: s.length, mean: s.reduce((x, y) => x + y, 0) / s.length, median: q(0.5), p95: q(0.95), p99: q(0.99), max: s[s.length - 1], min: s[0] };
    };
    const skip = Math.min(10, Math.floor(frames / 5)); // the first frames carry warm-up after camera jumps
    const iv = intervals.slice(skip);
    return {
        frames, degPerFrame: deg, sync, notify,
        canvas: [gl.drawingBufferWidth, gl.drawingBufferHeight],
        wallMs: wall,
        fpsMean: (iv.length / iv.reduce((x, y) => x + y, 0)) * 1000,
        interval: st(iv),
        skippedWarmup: skip,
        updateCpu: st(cpu.slice(skip)),
        gpuTimer: tq ? st(gpu.slice(skip)) : null,
        over16_7ms: iv.filter((x) => x > 16.7 * 1.5).length,
        statusMessagesSent: S1.sent.status - sentBefore,
        info: rendererInfo(),
    };
}

/** World box of each part's face meshes (vertices x matrixWorld). Fat edge lines are skipped: their
 * geometry is a unit template, which would drag the box to the part's origin. */
function partBoxes(paths: string[]) {
    const groups = (viewer!.nestedGroup as Any).groups as Record<string, THREE_NS.Object3D>;
    const out: Any = {};
    for (const p of paths) {
        const g = groups[p];
        if (!g) {
            out[p] = null;
            continue;
        }
        g.updateWorldMatrix(true, true);
        const box = new (T().Box3)();
        let meshes = 0;
        g.traverse((o: Any) => {
            if (o.isMesh && !o.isLineSegments2 && o.geometry?.attributes?.position) {
                box.expandByObject(o, true);
                meshes++;
            }
        });
        out[p] = { min: box.min.toArray(), max: box.max.toArray(), meshes, position: g.position.toArray(), quaternion: g.quaternion.toArray() };
    }
    return out;
}

function elementAt(x: number, y: number) {
    const el = document.elementFromPoint(x, y);
    return { isCanvas: el === viewer!.renderer.domElement, tag: el?.tagName ?? null, cls: (el as Any)?.className ?? null };
}

function rectOf(selector: string, text?: string) {
    const els = Array.from(document.querySelectorAll(selector)) as HTMLElement[];
    const el = text === undefined ? els[0] : els.find((e) => e.textContent?.trim() === text);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, visible: r.width > 0 && r.height > 0 };
}

/** The tree row's faces toggle for a leaf label (the tree is virtualised: only rows in view exist). */
function treeIconRect(label: string, icon = 0) {
    const labels = Array.from(document.querySelectorAll(".tv-node-label")) as HTMLElement[];
    const lab = labels.find((e) => (e.textContent ?? "").replace(/\s*⚈\s*$/, "").trim() === label);
    if (!lab) return null;
    const content = lab.closest(".tv-node-content") as HTMLElement;
    const ic = content?.querySelector(`.tv-icon${icon}`) as HTMLElement | null;
    if (!ic) return null;
    ic.scrollIntoView({ block: "center" });
    const r = ic.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
}

Object.assign(S1, {
    cameraState,
    project,
    setView,
    viewState,
    orbitTo,
    glInfo,
    rendererInfo,
    sceneStats,
    frameInfo,
    analyzePixels,
    bench,
    partBoxes,
    elementAt,
    rectOf,
    treeIconRect,
    findShapeEntry: (path: string) => !!findShapeEntry(viewer?.shapes, path),
    ready: () => !!viewer && viewer.ready === true,
    treeLabels: () => (Array.from(document.querySelectorAll(".tv-node-label")) as HTMLElement[]).map((e) => e.textContent?.trim()),
    getImage: async () => {
        const r = await viewer!.getImage("s1");
        return { task: r.task, length: typeof r.dataUrl === "string" ? r.dataUrl.length : 0, prefix: typeof r.dataUrl === "string" ? r.dataUrl.slice(0, 22) : null };
    },
    getImageData: async () => (await viewer!.getImage("s1")).dataUrl,
});

connect();
