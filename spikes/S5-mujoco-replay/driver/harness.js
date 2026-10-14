// Spike S5 measurement harness, injected into the STOCK ocp_viewer page with CDP
// Page.addScriptToEvaluateOnNewDocument. The served page is not modified.
//
// What it does, and why each is needed on the stock page:
// - The page keeps its Viewer in a closure (createPage does not return it). The page imports
//   three-cad-viewer as an ES module; a dynamic import of the same URL returns the same module
//   instance, so wrapping Viewer.prototype.render/update captures `this` the first time the page
//   renders (the splash, at onload).
// - comms.js hands each websocket message to window.postMessage; wrapping it timestamps receipt.
// - A second window "message" listener, added at DOMContentLoaded (after page.js has added its
//   own during module evaluation), runs right after the page has handled each message.
// - Outgoing frames (status updates with picks) are logged by wrapping WebSocket.prototype.send.
// - "s5_body_poses" messages (ours, relayed unchanged by the stock server's data channel) set
//   body group transforms directly: the update path a workbench page would add (slider, Q6).
(() => {
    const H = (window.__s5 = {
        viewer: null,
        events: [],
        outgoing: [],
        recvSeq: 0,
        lastRecv: null,
        moduleUrl: new URL("/static/js/three-cad-viewer.esm.js", location.href).href,
    });
    const wall = () => performance.timeOrigin + performance.now();
    H.wall = wall;

    // --- capture the Viewer -------------------------------------------------------------------
    // (`update` and `animate` are per-instance arrow functions; `render` is on the prototype.)
    H.ready = import(H.moduleUrl).then((m) => {
        H.mod = m;
        const orig = m.Viewer.prototype.render;
        m.Viewer.prototype.render = function (...args) {
            H.viewer = this;
            const t0 = wall();
            const r = orig.apply(this, args);
            H.events.push({ kind: "viewer.render", startWall: t0, endWall: wall() });
            return r;
        };
        return true;
    });

    // --- incoming ------------------------------------------------------------------------------
    const origPost = window.postMessage.bind(window);
    window.postMessage = function (message, ...rest) {
        H.recvSeq++;
        H.lastRecv = { seq: H.recvSeq, wall: wall(), chars: typeof message === "string" ? message.length : -1 };
        return origPost(message, ...rest);
    };

    function peek(text) {
        // type and our tag without parsing a 10 MB model twice: our tag is the last key
        if (typeof text !== "string") return { type: text?.type, s5: text?.s5 };
        let s5 = null;
        const m = /"s5":(\{[^{}]*\})\}\s*$/.exec(text.slice(-400));
        if (m) {
            try {
                s5 = JSON.parse(m[1]);
            } catch {
                s5 = null;
            }
        }
        // The top-level "type" is the LAST occurrence: a model's data part holds leaves'
        // "type":"shapes" before it, and neither config nor our tag has a "type" key. The config
        // can be long (tree states after the first show), so search the whole string.
        const i = text.lastIndexOf('"type":"');
        return { type: i >= 0 ? text.slice(i + 8, text.indexOf('"', i + 8)) : null, s5 };
    }

    function afterHandled(kind, s5, recv, extra = {}) {
        const handled = wall();
        const v = H.viewer;
        let gpuDone = null;
        if (v && v.renderer) {
            const gl = v.renderer.getContext();
            gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4)); // waits for the GPU
            gpuDone = wall();
        }
        const ev = { kind, s5, recvWall: recv?.wall ?? null, chars: recv?.chars ?? null, handledWall: handled, gpuDoneWall: gpuDone, ...extra };
        H.events.push(ev);
        requestAnimationFrame(() => {
            ev.rafWall = wall();
            ev.done = true;
        });
        return ev;
    }

    document.addEventListener("DOMContentLoaded", () => {
        window.addEventListener("message", (event) => {
            const recv = H.lastRecv;
            const { type, s5 } = peek(event.data);
            if (type === "s5_body_poses") {
                const msg = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
                const t0 = wall();
                applyPoses(msg.poses);
                const applied = wall();
                afterHandled(type, msg.s5, recv, { applyMs: applied - t0 });
                return;
            }
            if (type === "data" || type === "animation" || type === "set_relative_time" || type === "ui") {
                afterHandled(type, s5, recv);
            }
        });
    });

    // --- outgoing (status frames carry picks) -----------------------------------------------------
    const origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
        try {
            if (typeof data === "string" && data.startsWith("U:")) {
                const msg = JSON.parse(data.slice(2));
                const text = msg.text ?? {};
                const rec = { wall: wall(), command: msg.command, keys: typeof text === "object" ? Object.keys(text) : [] };
                if (text && text.lastPick) rec.lastPick = { path: text.lastPick.path, name: text.lastPick.name };
                H.outgoing.push(rec);
            }
        } catch {
            /* not ours to judge */
        }
        return origSend.call(this, data);
    };

    // --- helpers for the driver -------------------------------------------------------------------
    function groups() {
        return H.viewer.rendered.nestedGroup.groups;
    }

    function applyPoses(poses) {
        const g = groups();
        for (const [path, [t, q]] of Object.entries(poses)) {
            const grp = g[path];
            if (!grp) continue;
            grp.position.set(t[0], t[1], t[2]);
            grp.quaternion.set(q[0], q[1], q[2], q[3]);
        }
        H.viewer.update(true, false);
    }

    H.worldPoses = (paths) => {
        const T = window.THREE;
        const g = groups();
        const out = {};
        const p = new T.Vector3(), q = new T.Quaternion(), s = new T.Vector3();
        for (const path of paths) {
            const grp = g[path];
            if (!grp) {
                out[path] = null;
                continue;
            }
            grp.updateWorldMatrix(true, false);
            grp.matrixWorld.decompose(p, q, s);
            out[path] = { p: [p.x, p.y, p.z], q: [q.x, q.y, q.z, q.w], s: [s.x, s.y, s.z] };
        }
        return out;
    };

    H.rootWorld = () => {
        const root = H.viewer.rendered.nestedGroup.rootGroup;
        root.updateWorldMatrix(true, false);
        return Array.from(root.matrixWorld.elements);
    };

    H.animInfo = () => {
        const a = H.viewer.animation;
        const clip = a.clip;
        return {
            tracks: a.tracks.length,
            clipTracks: clip ? clip.tracks.length : 0,
            duration: a.duration,
            clipDuration: clip ? clip.duration : null,
            keyframes: clip && clip.tracks.length ? clip.tracks[0].times.length : 0,
            timesType: clip && clip.tracks.length ? clip.tracks[0].times.constructor.name : null,
            valuesType: clip && clip.tracks.length ? clip.tracks[0].values.constructor.name : null,
            names: a.tracks.slice(0, 6).map((t) => t.name),
            mode: H.viewer.state.get("animationMode"),
            running: !!a.clipAction?.isRunning(),
        };
    };

    // Set the viewer to keyframe k of the loaded clip through the public setRelativeTime and read
    // every body group's world transform. Keyframe times are the clip's own (float32) times.
    H.sampleKeyframes = (indices, paths, leafPaths = []) => {
        const a = H.viewer.animation;
        const times = a.clip.tracks[0].times;
        const out = [];
        for (const k of indices) {
            const t = times[k];
            H.viewer.setRelativeTime(t / a.duration);
            out.push({ k, tPage: t, actionTime: a.clipAction.time, poses: H.worldPoses(paths), leaves: H.worldPoses(leafPaths) });
        }
        return out;
    };

    H.leafPaths = (prefix) => Object.entries(groups()).filter(([id, g]) => id.startsWith(prefix) && g.shapeGeometry).map(([id]) => id);

    H.clickPlay = () => {
        const btn = document.querySelector(".tcv_play");
        if (!btn) return { ok: false, why: "no .tcv_play" };
        const r = btn.getBoundingClientRect();
        return { ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, visible: r.width > 0 && r.height > 0 };
    };

    // rAF cadence for `ms`, with the animation's own clock, renderer counters and per-frame
    // update() cost (wrapped for the duration).
    H.measureFrames = (ms) =>
        new Promise((resolve) => {
            const v = H.viewer;
            const a = v.animation;
            const intervals = [], actionTimes = [], updateMs = [];
            const origUpdate = v.update;
            v.update = function (...args) {
                const t0 = performance.now();
                const r = origUpdate.apply(this, args);
                updateMs.push(performance.now() - t0);
                return r;
            };
            let prev = null;
            const t0 = performance.now();
            const wall0 = wall();
            const step = (now) => {
                if (prev !== null) intervals.push(now - prev);
                prev = now;
                actionTimes.push([now - t0, a.clipAction ? a.clipAction.time : null]);
                if (now - t0 < ms) requestAnimationFrame(step);
                else {
                    v.update = origUpdate;
                    const info = v.renderer.info;
                    resolve({ wallStart: wall0, elapsedMs: now - t0, intervals, actionTimes, updateMs,
                        render: { calls: info.render.calls, triangles: info.render.triangles, lines: info.render.lines } });
                }
            };
            requestAnimationFrame(step);
        });

    H.glInfo = () => {
        const gl = H.viewer.renderer.getContext();
        const dbg = gl.getExtension("WEBGL_debug_renderer_info");
        return {
            version: gl.getParameter(gl.VERSION),
            renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
            canvas: [gl.drawingBufferWidth, gl.drawingBufferHeight],
            dpr: window.devicePixelRatio,
        };
    };

    H.project = (points) => {
        const T = window.THREE;
        const cam = H.viewer.camera.getCamera();
        cam.updateMatrixWorld();
        const rect = H.viewer.renderer.domElement.getBoundingClientRect();
        return points.map((p) => {
            const q = new T.Vector3(p[0], p[1], p[2]).project(cam);
            return [rect.left + ((q.x + 1) / 2) * rect.width, rect.top + ((1 - q.y) / 2) * rect.height, q.z];
        });
    };

    H.canvasRect = () => {
        const r = H.viewer.renderer.domElement.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
    };

    H.elementAt = (x, y) => {
        const el = document.elementFromPoint(x, y);
        return { isCanvas: el === H.viewer.renderer.domElement, tag: el?.tagName ?? null };
    };

    // Independent of the viewer's GPU id picking: a three.js ray from the camera through (x, y),
    // first hit among visible front-face meshes, reported as the owning leaf's id.
    H.raycast = (x, y) => {
        const T = window.THREE;
        const v = H.viewer;
        const cam = v.camera.getCamera();
        cam.updateMatrixWorld();
        const rect = v.renderer.domElement.getBoundingClientRect();
        const ndc = new T.Vector2(((x - rect.left) / rect.width) * 2 - 1, -(((y - rect.top) / rect.height) * 2 - 1));
        const rc = new T.Raycaster();
        rc.setFromCamera(ndc, cam);
        const fronts = [];
        v.rendered.nestedGroup.rootGroup.traverse((o) => {
            if (o.isMesh && o.material && o.material.name === "frontMaterial" && o.material.visible) fronts.push(o);
        });
        const hits = rc.intersectObjects(fronts, false);
        if (!hits.length) return null;
        const ids = Object.entries(v.rendered.nestedGroup.groups);
        const leafOf = (obj) => {
            for (let o = obj; o; o = o.parent) {
                const hit = ids.find(([, g]) => g === o);
                if (hit) return hit[0];
            }
            return null;
        };
        const first = leafOf(hits[0].object);
        const other = hits.find((h) => leafOf(h.object) !== first);
        return {
            id: first, point: hits[0].point.toArray(), distance: hits[0].distance,
            secondId: other ? leafOf(other.object) : null, secondGapMm: other ? other.distance - hits[0].distance : null,
        };
    };

    H.leafCenters = (prefix) => {
        const T = window.THREE;
        const out = {};
        for (const [id, g] of Object.entries(groups())) {
            if (!id.startsWith(prefix) || !g.shapeGeometry) continue;
            const b = new T.Box3().setFromObject(g, true);
            out[id] = { c: b.getCenter(new T.Vector3()).toArray(), size: b.getSize(new T.Vector3()).toArray() };
        }
        return out;
    };

    // on-screen extent (px) of a leaf's world bounding box
    H.leafScreenSize = (id) => {
        const T = window.THREE;
        const b = new T.Box3().setFromObject(groups()[id], true);
        const pts = [];
        for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) pts.push([x, y, z]);
        const s = H.project(pts);
        const xs = s.map((p) => p[0]), ys = s.map((p) => p[1]);
        return [Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)];
    };

    H.sceneCounts = () => {
        let meshes = 0, tris = 0;
        H.viewer.rendered.nestedGroup.rootGroup.traverse((o) => {
            if (o.isMesh && o.material?.name === "frontMaterial") {
                meshes++;
                const g = o.geometry;
                tris += g.index ? g.index.count / 3 : g.attributes.position.count / 3;
            }
        });
        return { frontMeshes: meshes, frontTriangles: tris, groups: Object.keys(groups()).length };
    };
})();
