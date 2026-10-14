// The one adapter to the stock viewer (three-cad-viewer through ocp-viewer-core's createPage), as
// spike S1 proved it: our page hosts the stock page, a Viewer subclass captures the instance
// createPage builds and re-attaches our overlay after every render, and messages go through
// `page.handleMessage`. Everything else in the page talks to the viewer through this module.
//
// Members used beyond the @public ones (S1's table): `nestedGroup.groups[path]` (move a part by
// transform), `scene` (overlay hook), `camera.getCamera()` (pick rays), `renderer` (canvas),
// `state.get("activeTab")`, `clipping.clipPlanes`, `shapes` (tessellation, tests only).

import { Display, Timer, Viewer, decodeInstancedFormat } from "three-cad-viewer";
import "three-cad-viewer/css";
import { createPage, type Page } from "ocp-viewer-core";
import type * as THREE_NS from "three";
import { Overlay, type PinItem } from "./overlay";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;
type Vec3 = [number, number, number];

export interface Settings {
    theme: string;
    tree_width: number;
    glass: boolean;
    tools: boolean;
    up: string;
    control: string;
    modifier_keys: Record<string, string>;
}

export interface Camera {
    position: number[];
    target: number[];
    quaternion: number[];
    zoom: number;
}

export interface ViewState {
    camera: Camera;
    hidden: string[];
    section?: { planes: { normal: number[]; offset: number }[]; intersection: boolean };
}

export const overlay = new Overlay();
let viewer: HostViewer | null = null;
let page: Page | null = null;
let root: string | null = null; // the scene's root path, e.g. /robot_assembly
let radius = 1000; // the model's bounding radius, for pin sizes
let hasModel = false;
let lastPointer: { x: number; y: number } | null = null;

class HostViewer extends Viewer {
    constructor(display: Any, options: Any, notifyCallback: Any, pinAsPngCallback?: Any, updateMarker?: boolean) {
        super(display, options, notifyCallback, pinAsPngCallback ?? null, updateMarker ?? true);
        viewer = this; // createPage does not return the instance it builds
    }

    render(shapes: Any, renderOptions: Any, viewerOptions: Any) {
        super.render(shapes, renderOptions, viewerOptions);
        overlay.attach(this.scene); // the scene is new after every render
    }
}

export function getViewer(): HostViewer | null {
    return viewer;
}

export function three(): typeof THREE_NS {
    return (window as Any).THREE;
}

/** Mount the stock page into `container`; `send` is createPage's (command, message) channel. */
export function mount(container: HTMLElement, settings: Settings, send: (command: string, message: Any) => void) {
    // Pointer position of the last click on the canvas, captured before the viewer's own handler
    // commits a pick (S1): the pick's world point is the ray through it.
    for (const kind of ["pointerup", "dblclick"]) {
        container.addEventListener(kind, (e) => {
            const ev = e as MouseEvent;
            lastPointer = { x: ev.clientX, y: ev.clientY };
        }, true);
    }
    page = createPage({
        Viewer: HostViewer,
        Display,
        Timer,
        send,
        theme: settings.theme,
        container,
        listen: false,
        getSize: () => ({ width: container.clientWidth, height: container.clientHeight }),
        overrides: {
            display: { treeWidth: settings.tree_width, glass: settings.glass, tools: settings.tools, theme: settings.theme, zscaleTool: false },
            viewer: { tools: settings.tools, glass: settings.glass, up: settings.up, control: settings.control },
        },
    });
    page.showSplash({ theme: settings.theme, treeWidth: settings.tree_width, keymap: settings.modifier_keys });
    new ResizeObserver(() => page?.resize()).observe(container);
}

/** The GL renderer the page got (spike S1: headless and misconfigured windows fall back to SwiftShader). */
export function rendererName(): string | null {
    const gl = viewer?.renderer.getContext() as WebGL2RenderingContext | undefined;
    if (!gl) return null;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    return String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
}

export function handleMessage(message: Any) {
    page?.handleMessage(message);
}

// -- scenes ----------------------------------------------------------------------------------------

/** Show a full data message (a kernel blob). A page's first model fits the camera, as the stock
 * pipeline decides for a page that still shows the logo. */
export function showModel(payload: Any, sceneRoot: string) {
    if (!page) throw new Error("the viewer is not mounted");
    if (!hasModel && payload.config) payload.config.resetCamera = "reset";
    const bb = payload?.data?.shapes?.bb;
    if (bb) radius = Math.max(1, Math.hypot(bb.xmax - bb.xmin, bb.ymax - bb.ymin, bb.zmax - bb.zmin) / 2);
    page.handleMessage(payload);
    root = sceneRoot;
    hasModel = true;
}

/** Apply an incremental update: moved parts by transform, changed parts through updatePart.
 * Returns false if a path is unknown (the caller then asks for the whole scene again). */
export function applyUpdate(moved: { path: string; loc: [Vec3, number[]] }[], changed: { path: string; label: string; data: Any }[]): boolean {
    const v = viewer;
    if (!v) return false;
    const groups = (v.nestedGroup as Any).groups as Record<string, THREE_NS.Object3D>;
    if ([...moved, ...changed].some((m) => !groups[m.path])) return false;
    for (const c of changed) {
        const tree = decodeInstancedFormat(c.data) as Any;
        const leaf = tree.parts[0];
        leaf.name = c.label;
        leaf.id = c.path;
        v.updatePart(c.path, leaf, { skipBounds: true });
        if (leaf.loc) setLoc(groups[c.path], leaf.loc);
    }
    for (const m of moved) setLoc(groups[m.path], m.loc);
    v.updateBounds(); // bounds, pick buffer, clipping, re-render
    return true;
}

function setLoc(group: THREE_NS.Object3D, loc: [number[], number[]]) {
    const [t, q] = loc;
    group.position.set(t[0], t[1], t[2]);
    group.quaternion.set(q[0], q[1], q[2], q[3]);
}

// -- camera, picks, views ------------------------------------------------------------------------

export function cameraState(): Camera {
    const v = viewer!;
    return { position: v.getCameraPosition(), target: [...v.getCameraTarget()], quaternion: [...v.getCameraQuaternion()], zoom: v.getCameraZoom() };
}

/** The part path an id belongs to: /root/part of /root/part/faces/faces_3. */
export function partPath(id: string): string {
    return id.split("/").slice(0, 3).join("/");
}

export function labelOf(path: string): string {
    return path.split("/")[2] ?? "";
}

function clipped(p: THREE_NS.Vector3): boolean {
    const v = viewer!;
    if ((v as Any).state?.get?.("activeTab") !== "clip") return false;
    const planes = ((v as Any).clipping?.clipPlanes ?? []) as THREE_NS.Plane[];
    if (!planes.length) return false;
    const outside = planes.map((plane) => plane.distanceToPoint(p) < 0);
    return v.getClipIntersection() ? outside.every(Boolean) : outside.some(Boolean);
}

/** The first visible surface point of `objects` under a page pixel, with its normal. */
export function raycast(x: number, y: number, objects?: THREE_NS.Object3D[]): { point: Vec3; normal: Vec3 | null; object: THREE_NS.Object3D } | null {
    const v = viewer;
    if (!v) return null;
    const T = three();
    const rect = v.renderer.domElement.getBoundingClientRect();
    const ndc = new T.Vector2(((x - rect.left) / rect.width) * 2 - 1, -((y - rect.top) / rect.height) * 2 + 1);
    const ray = new T.Raycaster();
    ray.setFromCamera(ndc, v.camera.getCamera());
    const meshes: THREE_NS.Object3D[] = [];
    const groups = (v.nestedGroup as Any).groups as Record<string, THREE_NS.Object3D>;
    for (const start of objects ?? Object.values(groups)) {
        start.traverse((o: Any) => {
            if (o.isMesh && !o.isLineSegments2 && o.geometry?.attributes?.position && visible(o)) meshes.push(o);
        });
    }
    const hits = ray.intersectObjects(meshes, false).filter((h) => !clipped(h.point));
    if (!hits.length) return null;
    const h = hits[0];
    const n = h.face ? h.face.normal.clone().transformDirection(h.object.matrixWorld) : null;
    if (n && n.dot(ray.ray.direction) > 0) n.negate(); // the side facing the camera
    return { point: h.point.toArray() as Vec3, normal: n ? (n.toArray() as Vec3) : null, object: h.object };
}

/** Drawn: the object and its parents visible, and its material too (the viewer hides a part from
 * the tree by setting `material.visible`, not the mesh's own flag). */
function visible(o: THREE_NS.Object3D): boolean {
    const material = (o as Any).material;
    if (material && !Array.isArray(material) && material.visible === false) return false;
    for (let p: THREE_NS.Object3D | null = o; p; p = p.parent) if (!p.visible) return false;
    return true;
}

/** What a status message tells about a pick, with the context the hub keeps: the camera and the
 * picked point on the part (a ray through the clicked pixel, against that part's own meshes). */
export function pickContext(status: Any): Any {
    let id: string | null = null;
    const ids = status?.selectedShapeIDs;
    if (Array.isArray(ids)) {
        const strings = ids.filter((i: unknown) => typeof i === "string");
        if (strings.length) id = strings[strings.length - 1];
    }
    const last = status?.lastPick;
    if (last && typeof last.path === "string" && typeof last.name === "string") id = `${last.path}/${last.name}`;
    if (!id || !viewer) return undefined;
    const ctx: Any = { camera: cameraState() };
    const group = ((viewer.nestedGroup as Any).groups as Record<string, THREE_NS.Object3D>)[partPath(id)];
    if (lastPointer && group) {
        const hit = raycast(lastPointer.x, lastPointer.y, [group]);
        if (hit) {
            ctx.point = hit.point;
            ctx.normal = hit.normal;
        }
    }
    return ctx;
}

export function currentView(): ViewState {
    const v = viewer!;
    const states = v.getStates();
    const shown = new Set<string>();
    const hiddenLeaves = new Set<string>();
    for (const [path, st] of Object.entries(states)) {
        const label = labelOf(path);
        if (st[0] === 0 && st[1] === 0) hiddenLeaves.add(label);
        else shown.add(label);
    }
    const view: ViewState = { camera: cameraState(), hidden: [...hiddenLeaves].filter((l) => !shown.has(l)) };
    if ((v as Any).state?.get?.("activeTab") === "clip") {
        view.section = {
            planes: ([0, 1, 2] as const).map((i) => ({ normal: [...v.getClipNormal(i)], offset: v.getClipSlider(i) })),
            intersection: v.getClipIntersection(),
        };
    }
    return view;
}

/** Apply a saved view. `hidden` undefined leaves visibility alone; `section` null turns clipping
 * off, undefined leaves it alone (a comment's camera only moves the camera). */
export function applyView(view: { camera: Camera; hidden?: string[]; section?: ViewState["section"] | null }) {
    const v = viewer!;
    if (view.hidden !== undefined) {
        const states = v.getStates();
        const hidden = new Set(view.hidden);
        const next: Record<string, [number, number]> = {};
        for (const path of Object.keys(states)) next[path] = hidden.has(labelOf(path)) ? [0, 0] : [1, 1];
        v.setStates(next as Any);
    }
    if (view.section) {
        v.setActiveTab("clip", true);
        view.section.planes.forEach((p, i) => v.setClipNormal(i as 0 | 1 | 2, p.normal as Vec3, p.offset, true));
        v.setClipIntersection(!!view.section.intersection, true);
    } else if (view.section === null && (v as Any).state?.get?.("activeTab") === "clip") {
        v.setActiveTab("tree", true);
    }
    const c = view.camera;
    v.setCameraLocationSettings(c.position as Vec3, (c.quaternion ?? null) as Any, c.target as Vec3, c.zoom ?? null, true);
}

export async function thumbnail(): Promise<string> {
    const result = await viewer!.getImage("comment");
    return result.dataUrl as string;
}

// -- pins ------------------------------------------------------------------------------------------

export function setPins(pins: { item: string; point: number[]; normal: number[] | null }[], selected: string | null) {
    if (!viewer || !hasModel) return;
    const items: PinItem[] = pins.map((p) => ({
        id: p.item,
        point: p.point as Vec3,
        normal: (p.normal ?? [0, 0, 1]) as Vec3,
        length: radius * 0.08,
        head: radius * 0.012,
        selected: p.item === selected,
    }));
    overlay.setPins(items);
    viewer.update(true, false);
}

export function sceneRoot(): string | null {
    return root;
}
