// window.__wb: what the end-to-end driver reads and drives over CDP (e2e/). Read-only views of the
// page and the viewer, plus helpers that find pickable pixels from the page's own tessellation and
// three.js ray casts, so the driver can click a face, an edge or a vertex it knows.

import { appliedLog, received } from "./hub";
import { getStore } from "./store";
import * as host from "./viewer/host";
import { overlay } from "./viewer/host";
import type * as THREE_NS from "three";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;
type Vec3 = [number, number, number];

function v(): Any {
    const viewer = host.getViewer();
    if (!viewer) throw new Error("no viewer yet");
    return viewer;
}

function groups(): Record<string, THREE_NS.Object3D> {
    return v().nestedGroup.groups;
}

function project(points: number[][]): number[][] {
    const cam = v().camera.getCamera();
    cam.updateMatrixWorld();
    const rect = v().renderer.domElement.getBoundingClientRect();
    const T = host.three();
    return points.map((p) => {
        const q = new T.Vector3(p[0], p[1], p[2]).project(cam);
        return [rect.left + ((q.x + 1) / 2) * rect.width, rect.top + ((1 - q.y) / 2) * rect.height, q.z];
    });
}

/** Look at `target` from `position` (Z up) through the viewer's camera API. */
function setView(position: number[], target: number[], zoom: number | null = null) {
    const T = host.three();
    const d = new T.OrthographicCamera();
    d.up.set(0, 0, 1);
    d.position.set(position[0], position[1], position[2]);
    d.lookAt(target[0], target[1], target[2]);
    v().setCameraLocationSettings(position, d.quaternion.toArray(), target, zoom, true);
    return host.cameraState();
}

/** Show only the leaves of part `label` (true) or everything (false). */
function isolate(label: string | null) {
    const states = v().getStates();
    const next: Record<string, number[]> = {};
    for (const path of Object.keys(states)) next[path] = label === null || host.labelOf(path) === label ? [1, 1] : [0, 0];
    v().setStates(next);
}

interface Leaf {
    path: string;
    shape: Any;
    matrix: THREE_NS.Matrix4;
}

function leavesOf(label: string): Leaf[] {
    const out: Leaf[] = [];
    const walk = (node: Any) => {
        if (node.parts) node.parts.forEach(walk);
        else if (node.shape && host.labelOf(node.id) === label) {
            const g = groups()[node.id];
            g.updateWorldMatrix(true, false);
            out.push({ path: node.id, shape: node.shape, matrix: g.matrixWorld.clone() });
        }
    };
    walk(v().shapes);
    return out;
}

function toWorld(m: THREE_NS.Matrix4, x: number, y: number, z: number): Vec3 {
    const p = new (host.three().Vector3)(x, y, z).applyMatrix4(m);
    return [p.x, p.y, p.z];
}

/** Edges (as world polylines) and vertices (world points) of a part's tessellation. */
function topology(label: string) {
    const edges: Vec3[][] = [];
    const vertices: Vec3[] = [];
    for (const leaf of leavesOf(label)) {
        const flat = Array.from(leaf.shape.edges as ArrayLike<number>);
        const spe = Array.from((leaf.shape.segments_per_edge ?? []) as ArrayLike<number>);
        let k = 0;
        for (const n of spe) {
            const pts: Vec3[] = [];
            for (let s = 0; s < n; s++, k += 6) {
                if (s === 0) pts.push(toWorld(leaf.matrix, flat[k], flat[k + 1], flat[k + 2]));
                pts.push(toWorld(leaf.matrix, flat[k + 3], flat[k + 4], flat[k + 5]));
            }
            edges.push(pts);
        }
        const ov = Array.from(leaf.shape.obj_vertices as ArrayLike<number>);
        for (let i = 0; i + 2 < ov.length; i += 3) vertices.push(toWorld(leaf.matrix, ov[i], ov[i + 1], ov[i + 2]));
    }
    return { edges, vertices };
}

function segDist(p: number[], a: number[], b: number[]): number {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy;
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2));
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/** Pixel distance from `p` to the projected polylines, skipping polyline `skip`. */
function distToEdges(p: number[], projected: number[][][], skip = -1): number {
    let best = Infinity;
    projected.forEach((poly, i) => {
        if (i === skip) return;
        for (let s = 0; s + 1 < poly.length; s++) best = Math.min(best, segDist(p, poly[s], poly[s + 1]));
    });
    return best;
}

function onPart(hit: Any, label: string): boolean {
    const mine = new Set(Object.entries(groups()).filter(([path]) => host.labelOf(path) === label).map(([, g]) => g));
    for (let o = hit?.object; o; o = o.parent) if (mine.has(o)) return true;
    return false;
}

function canvasRect() {
    return v().renderer.domElement.getBoundingClientRect();
}

/** A pixel on a face of `label`, as far from the part's projected edges as possible. */
function findFaceTarget(label: string) {
    const topo = topology(label);
    const projected = topo.edges.map((e) => project(e));
    const all = projected.flat();
    const r = canvasRect();
    const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
    const x0 = Math.max(r.left + 2, Math.min(...xs)), x1 = Math.min(r.right - 2, Math.max(...xs));
    const y0 = Math.max(r.top + 2, Math.min(...ys)), y1 = Math.min(r.bottom - 2, Math.max(...ys));
    const step = Math.max(4, Math.round(Math.max(x1 - x0, y1 - y0) / 60)); // about 3600 samples at most
    let best: Any = null;
    for (let y = y0; y <= y1; y += step) {
        for (let x = x0; x <= x1; x += step) {
            if (document.elementFromPoint(x, y) !== v().renderer.domElement) continue;
            const hit = host.raycast(x, y);
            if (!hit || !onPart(hit, label)) continue;
            const d = distToEdges([x, y], projected);
            if (!best || d > best.edgeDistancePx) best = { x: Math.round(x), y: Math.round(y), point: hit.point, normal: hit.normal, edgeDistancePx: d };
        }
    }
    return best;
}

/** The midpoint pixel of the longest visible edge of `label` that is clear of its vertices and of
 * other edges, so the viewer's picker (vertex over edge over face) picks that edge. With `strict`
 * false, the longest visible edge regardless of clearance (to zoom in on). */
function findEdgeTarget(label: string, strict = true) {
    const topo = topology(label);
    const projected = topo.edges.map((e) => project(e));
    const projVerts = project(topo.vertices);
    const candidates: Any[] = [];
    projected.forEach((poly, i) => {
        if (poly.length < 2) return;
        // the polyline's middle point
        const mid = poly[Math.floor((poly.length - 1) / 2)];
        const midNext = poly[Math.ceil((poly.length - 1) / 2)];
        const p = poly.length % 2 === 1 ? mid : [(mid[0] + midNext[0]) / 2, (mid[1] + midNext[1]) / 2, (mid[2] + midNext[2]) / 2];
        const lengthPx = poly.slice(1).reduce((acc, q, k) => acc + Math.hypot(q[0] - poly[k][0], q[1] - poly[k][1]), 0);
        const vertexPx = Math.min(...projVerts.map((q) => Math.hypot(q[0] - p[0], q[1] - p[1])));
        const otherEdgePx = distToEdges(p, projected, i);
        if (document.elementFromPoint(p[0], p[1]) !== v().renderer.domElement) return;
        // visible: nothing of the model in front of the edge's own depth at that pixel
        const pts = topo.edges[i];
        const a = pts[Math.floor((pts.length - 1) / 2)], b = pts[Math.ceil((pts.length - 1) / 2)];
        const world = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2] as Vec3; // the same middle as p
        const hit = host.raycast(p[0], p[1]);
        const cam = v().camera.getCamera();
        const dir = new (host.three().Vector3)();
        cam.getWorldDirection(dir);
        const depth = (q: number[]) => q[0] * dir.x + q[1] * dir.y + q[2] * dir.z;
        if (hit && depth(hit.point) < depth(world) - 0.5) return;
        candidates.push({ index: i, x: Math.round(p[0]), y: Math.round(p[1]), lengthPx, vertexPx, otherEdgePx, world: topo.edges[i], mid: world });
    });
    const good = candidates.filter((c) => !strict || (c.vertexPx >= 15 && c.otherEdgePx >= 8)).sort((a, b) => b.lengthPx - a.lengthPx);
    const best = good[0];
    return best
        ? { x: best.x, y: best.y, start: best.world[0], end: best.world[best.world.length - 1], mid: best.mid, lengthPx: best.lengthPx, vertexPx: best.vertexPx, otherEdgePx: best.otherEdgePx }
        : null;
}

/** The visible vertex of `label` farthest from the part's other vertices on screen; null if, with
 * `minClearPx`, none is that far (zoom in first). */
function findVertexTarget(label: string, minClearPx = 0) {
    const topo = topology(label);
    const projVerts = project(topo.vertices);
    const cam = v().camera.getCamera();
    const dir = new (host.three().Vector3)();
    cam.getWorldDirection(dir);
    const depth = (q: number[]) => q[0] * dir.x + q[1] * dir.y + q[2] * dir.z;
    let best: Any = null;
    projVerts.forEach((p, i) => {
        if (document.elementFromPoint(p[0], p[1]) !== v().renderer.domElement) return;
        const hit = host.raycast(p[0], p[1]);
        if (hit && depth(hit.point) < depth(topo.vertices[i]) - 0.5) return;
        const others = Math.min(...projVerts.filter((_, j) => j !== i).map((q) => Math.hypot(q[0] - p[0], q[1] - p[1])));
        if (!best || others > best.otherVertexPx) best = { x: Math.round(p[0]), y: Math.round(p[1]), point: topo.vertices[i], otherVertexPx: others };
    });
    return best && best.otherVertexPx >= minClearPx ? best : null;
}

/** Aim at a world point keeping the view direction, at `pxPerMm` screen pixels per mm. */
function focus(point: number[], pxPerMm: number) {
    const cam = v().camera.getCamera();
    const zoom = (pxPerMm * (cam.top - cam.bottom)) / canvasRect().height;
    const now = host.cameraState();
    return setView(point.map((c, k) => c + now.position[k] - now.target[k]), point, zoom);
}

/** Bounding box of a part's tessellation in world coordinates. */
function partBox(label: string) {
    const topo = topology(label);
    const pts = [...topo.vertices, ...topo.edges.flat()];
    const lo = [0, 1, 2].map((k) => Math.min(...pts.map((p) => p[k])));
    const hi = [0, 1, 2].map((k) => Math.max(...pts.map((p) => p[k])));
    return { min: lo, max: hi, centre: lo.map((x, k) => (x + hi[k]) / 2) };
}

/** Aim the camera at part `label` from azimuth/elevation (degrees), the part filling `fill` of the
 * view's height; the camera keeps its distance to the target. */
function frame(label: string, az: number, el: number, fill = 0.6) {
    const box = partBox(label);
    const size = Math.max(...box.max.map((x, k) => x - box.min[k]), 1e-6);
    const cam = v().camera.getCamera();
    const zoom = Math.min(40, Math.max(0.05, (fill * (cam.top - cam.bottom)) / size));
    const now = host.cameraState();
    const dist = Math.hypot(...now.position.map((x, k) => x - now.target[k]));
    const a = (az * Math.PI) / 180, e = (el * Math.PI) / 180;
    const dir = [Math.cos(e) * Math.cos(a), Math.cos(e) * Math.sin(a), Math.sin(e)];
    return setView(box.centre.map((c, k) => c + dir[k] * dist), box.centre, zoom);
}

function glInfo() {
    const gl = v().renderer.getContext() as WebGL2RenderingContext;
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    return { renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
}

function rectOf(selector: string, text?: string) {
    const els = Array.from(document.querySelectorAll(selector)) as HTMLElement[];
    const el = text === undefined ? els[0] : els.find((e) => e.textContent?.trim() === text);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, visible: r.width > 0 && r.height > 0 };
}

(window as Any).__wb = {
    get viewer() {
        return host.getViewer();
    },
    store: getStore,
    received,
    appliedLog,
    overlay,
    cameraState: () => host.cameraState(),
    project,
    setView,
    isolate,
    partBox,
    frame,
    focus,
    activeTool: () => v().state.get("activeTool"),
    findFaceTarget,
    findEdgeTarget,
    findVertexTarget,
    glInfo,
    rectOf,
    elementAtIsCanvas: (x: number, y: number) => document.elementFromPoint(x, y) === v().renderer.domElement,
    render: () => v().update(true, false),
    leafCount: () => Object.keys(v().getStates()).length,
};
