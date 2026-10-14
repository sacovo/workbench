// Our own overlay: a second three.js scene drawn by the viewer's own WebGLRenderer, into the
// same canvas and GL context, right after the CAD scene and before anything clears depth.
//
// Hook: three.js calls `scene.onAfterRender(renderer, scene, camera)` at the end of every
// `renderer.render(scene, camera)`. The viewer re-creates its scene on every model, so the
// hook is re-attached after each `Viewer.render` (HostViewer does that).
//
// The CAD depth buffer is still intact at that point, so the overlay is depth-tested against
// the model: pins behind geometry are hidden. (Drawing from `viewer.onAfterRender` instead
// would be too late: `update()` clears depth for the orientation marker before calling it.)
//
// The viewer renders the same scene into its GPU pick target (with an override material and
// pick layers) and, in Studio mode, into composer targets. The overlay only draws into the
// default framebuffer, so it never pollutes the pick buffer.

import type * as THREE_NS from "three";

type Vec3 = [number, number, number];

export interface PinItem {
    kind: "pin";
    id: string;
    point: Vec3;
    normal: Vec3;
    length: number; // mm
    head: number; // head radius, mm
}

export interface ArrowItem {
    kind: "arrow";
    id: string;
    tip: Vec3;
    dir: Vec3; // from tail to tip
    length: number; // mm
    radius: number; // shaft radius, mm
}

export type OverlayItem = PinItem | ArrowItem;

// Colours that occur nowhere in the model, so pixels can be attributed in tests.
export const COLORS = {
    pinHead: 0xff00ff,
    pinNeedle: 0xff0080,
    arrow: 0x00ffff,
};

function three(): typeof THREE_NS {
    // three-cad-viewer bundles its own three.js and publishes it as window.THREE in the Viewer
    // constructor. Using that copy keeps one three.js instance in the page.
    const t = (window as unknown as { THREE?: typeof THREE_NS }).THREE;
    if (!t) throw new Error("window.THREE not set: create the viewer first");
    return t;
}

export class Overlay {
    scene: THREE_NS.Scene | null = null;
    private drawing = false;
    depthTest = true;
    enabled = true;
    items: OverlayItem[] = [];
    draws = 0;
    skippedTargets = 0;
    lastDrawMs = 0;

    /** Attach to the viewer's current scene (call after every Viewer.render). */
    attach(scene: THREE_NS.Scene) {
        const T = three();
        if (!this.scene) this.scene = new T.Scene();
        scene.onAfterRender = (renderer, _scene, camera) => this.draw(renderer, camera);
    }

    private draw(renderer: THREE_NS.WebGLRenderer, camera: THREE_NS.Camera) {
        if (!this.enabled || this.drawing || !this.scene || this.scene.children.length === 0) return;
        if (renderer.getRenderTarget() !== null) {
            // pick buffer or a composer pass: not ours to draw into
            this.skippedTargets++;
            return;
        }
        this.drawing = true;
        const t0 = performance.now();
        try {
            renderer.render(this.scene, camera);
            this.draws++;
        } finally {
            this.drawing = false;
            this.lastDrawMs = performance.now() - t0;
        }
    }

    setItems(items: OverlayItem[]) {
        const T = three();
        if (!this.scene) this.scene = new T.Scene();
        for (const child of [...this.scene.children]) {
            this.scene.remove(child);
            child.traverse((o) => {
                const m = o as THREE_NS.Mesh;
                if (m.geometry) m.geometry.dispose();
                if (m.material) (m.material as THREE_NS.Material).dispose();
            });
        }
        this.items = items;
        for (const item of items) {
            const obj = item.kind === "pin" ? this.makePin(item) : this.makeArrow(item);
            obj.name = item.id;
            obj.traverse((o) => o.layers.enableAll());
            this.scene.add(obj);
        }
        this.applyDepthTest();
    }

    setDepthTest(flag: boolean) {
        this.depthTest = flag;
        this.applyDepthTest();
    }

    private applyDepthTest() {
        this.scene?.traverse((o) => {
            const m = (o as THREE_NS.Mesh).material as THREE_NS.Material | undefined;
            if (m) {
                m.depthTest = this.depthTest;
                m.needsUpdate = true;
            }
        });
    }

    private material(color: number) {
        const T = three();
        // Unlit, so the colour on screen is exactly the colour given (pixel tests rely on it).
        return new T.MeshBasicMaterial({ color, depthTest: true, depthWrite: true });
    }

    private makePin(p: PinItem) {
        const T = three();
        const n = new T.Vector3(...p.normal).normalize();
        const base = new T.Vector3(...p.point);
        const g = new T.Group();
        const needle = new T.Mesh(new T.CylinderGeometry(p.head * 0.25, p.head * 0.25, p.length, 12), this.material(COLORS.pinNeedle));
        needle.position.copy(base.clone().addScaledVector(n, p.length / 2));
        needle.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), n);
        const head = new T.Mesh(new T.SphereGeometry(p.head, 24, 16), this.material(COLORS.pinHead));
        head.position.copy(base.clone().addScaledVector(n, p.length + p.head));
        head.name = `${p.id}:head`;
        g.add(needle, head);
        return g;
    }

    private makeArrow(a: ArrowItem) {
        const T = three();
        const d = new T.Vector3(...a.dir).normalize();
        const tip = new T.Vector3(...a.tip);
        const coneLen = a.radius * 5;
        const shaftLen = Math.max(0, a.length - coneLen);
        const g = new T.Group();
        const cone = new T.Mesh(new T.ConeGeometry(a.radius * 2.2, coneLen, 20), this.material(COLORS.arrow));
        cone.position.copy(tip.clone().addScaledVector(d, -coneLen / 2));
        cone.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), d);
        const shaft = new T.Mesh(new T.CylinderGeometry(a.radius, a.radius, shaftLen, 12), this.material(COLORS.arrow));
        shaft.position.copy(tip.clone().addScaledVector(d, -coneLen - shaftLen / 2));
        shaft.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), d);
        g.add(cone, shaft);
        return g;
    }

    /** World centre of a pin's head, for the glue test. */
    headCentre(id: string): Vec3 | null {
        const head = this.scene?.getObjectByName(`${id}:head`);
        if (!head) return null;
        const v = new (three().Vector3)();
        head.getWorldPosition(v);
        return [v.x, v.y, v.z];
    }
}
