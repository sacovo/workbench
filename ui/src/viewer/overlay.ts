// Our own overlay (spike S1): a second three.js scene drawn by the viewer's own WebGLRenderer, into
// the same canvas and GL context, right after the CAD scene and before anything clears depth.
//
// Hook: three.js calls `scene.onAfterRender(renderer, scene, camera)` at the end of every
// `renderer.render(scene, camera)`. The viewer re-creates its scene on every model, so the hook is
// re-attached after each `Viewer.render` (HostViewer does that). The CAD depth buffer is still
// intact then, so pins behind geometry are hidden. Passes into render targets (the GPU pick buffer,
// Studio composer passes) are skipped, so the overlay never pollutes picking.

import type * as THREE_NS from "three";

type Vec3 = [number, number, number];

export interface PinItem {
    id: string;
    point: Vec3;
    normal: Vec3;
    length: number; // mm
    head: number; // head radius, mm
    selected?: boolean;
}

export const COLORS = { head: 0xd6247a, headSelected: 0xffb000, needle: 0x7a1446 };

function three(): typeof THREE_NS {
    // three-cad-viewer bundles its own three.js and publishes it as window.THREE in the Viewer
    // constructor; using that copy keeps one three.js instance in the page.
    const t = (window as unknown as { THREE?: typeof THREE_NS }).THREE;
    if (!t) throw new Error("window.THREE not set: create the viewer first");
    return t;
}

export class Overlay {
    scene: THREE_NS.Scene | null = null;
    private drawing = false;
    items: PinItem[] = [];
    draws = 0;

    /** Attach to the viewer's current scene (call after every Viewer.render). */
    attach(scene: THREE_NS.Scene) {
        if (!this.scene) this.scene = new (three().Scene)();
        scene.onAfterRender = (renderer, _scene, camera) => this.draw(renderer, camera);
    }

    private draw(renderer: THREE_NS.WebGLRenderer, camera: THREE_NS.Camera) {
        if (this.drawing || !this.scene || this.scene.children.length === 0) return;
        if (renderer.getRenderTarget() !== null) return; // pick buffer or a composer pass
        this.drawing = true;
        try {
            renderer.render(this.scene, camera);
            this.draws++;
        } finally {
            this.drawing = false;
        }
    }

    setPins(items: PinItem[]) {
        const T = three();
        if (!this.scene) this.scene = new T.Scene();
        for (const child of [...this.scene.children]) {
            this.scene.remove(child);
            child.traverse((o) => {
                const m = o as THREE_NS.Mesh;
                m.geometry?.dispose();
                (m.material as THREE_NS.Material | undefined)?.dispose();
            });
        }
        this.items = items;
        for (const p of items) {
            const n = new T.Vector3(...p.normal).normalize();
            const base = new T.Vector3(...p.point);
            const g = new T.Group();
            g.name = p.id;
            const material = (color: number) => new T.MeshBasicMaterial({ color, depthTest: true, depthWrite: true });
            const needle = new T.Mesh(new T.CylinderGeometry(p.head * 0.25, p.head * 0.25, p.length, 12), material(COLORS.needle));
            needle.position.copy(base.clone().addScaledVector(n, p.length / 2));
            needle.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), n);
            const head = new T.Mesh(new T.SphereGeometry(p.head, 24, 16), material(p.selected ? COLORS.headSelected : COLORS.head));
            head.position.copy(base.clone().addScaledVector(n, p.length + p.head));
            head.name = `${p.id}:head`;
            g.add(needle, head);
            g.traverse((o) => o.layers.enableAll());
            this.scene.add(g);
        }
    }

    /** World centre of a pin's head (tests). */
    headCentre(id: string): Vec3 | null {
        const head = this.scene?.getObjectByName(`${id}:head`);
        if (!head) return null;
        const v = new (three().Vector3)();
        head.getWorldPosition(v);
        return [v.x, v.y, v.z];
    }
}
