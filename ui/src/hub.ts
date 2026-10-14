// The page's WebSocket to the hub (/ws?token=...): what the hub pushes, and what the stock viewer
// reports going back. Scenes and updates are applied one after another, in the order they came.
//
// Hub -> page: wb_hello (settings, state), wb_state, wb_review, wb_scene, wb_update, wb_backend
//              (the stock measure backend's answer), wb_pick (the resolved selection), wb_notice.
// Page -> hub: status (the stock viewer's status messages, with the scene shown and a pick's
//              camera and point), shown (a scene was drawn), rebuild, resync, log.

import { blob, token } from "./api";
import { getStore, notify, setStore } from "./store";
import * as viewer from "./viewer/host";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

let ws: WebSocket | null = null;
let chain: Promise<void> = Promise.resolve();
let mounted = false;
let resolveMounted: () => void;
const viewerMounted = new Promise<void>((r) => (resolveMounted = r));

// What arrived, for the end-to-end driver (window.__wb.received).
export const received: { type: string; bytes: number; t: number; scene?: string }[] = [];
export const appliedLog: { type: string; scene: string; ms: number; t: number }[] = [];

export function send(message: Any) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

/** createPage's channel out: status messages carry the scene shown and a pick's context. */
export function fromViewer(command: string, message: Any) {
    if (command === "status") {
        const shown = getStore().shown;
        send({ type: "status", scene: shown?.scene ?? null, status: message, pick: viewer.pickContext(message) });
    } else if (command === "log") {
        send({ type: "log", text: String(message).slice(0, 2000) });
    }
}

export function mountViewer(container: HTMLElement) {
    const settings = getStore().settings;
    if (mounted || !settings) return;
    mounted = true;
    viewer.mount(container, settings as unknown as viewer.Settings, fromViewer);
    setStore({ renderer: viewer.rendererName() });
    resolveMounted();
}

function enqueue(step: () => Promise<void>) {
    chain = chain.then(() => viewerMounted).then(step).catch((e) => notify("error", `viewer: ${e?.message ?? e}`));
}

async function showScene(msg: Any) {
    const t0 = performance.now();
    const payload = await blob<Any>(msg.blob.sha);
    viewer.showModel(payload, msg.root);
    setStore({ shown: { scene: msg.scene, assembly: msg.assembly } });
    shownDone("wb_scene", msg.scene, t0);
}

async function applyUpdate(msg: Any) {
    const t0 = performance.now();
    const shown = getStore().shown;
    if (!shown || shown.scene !== msg.previous) {
        send({ type: "resync" });
        return;
    }
    let changed: Any[] = [];
    if (msg.changed.length) {
        const parts = (await blob<Any>(msg.blob.sha)).parts;
        changed = msg.changed.map((c: Any) => ({ ...c, data: parts[c.path] }));
    }
    if (!viewer.applyUpdate(msg.moved, changed)) {
        send({ type: "resync" });
        return;
    }
    setStore({ shown: { scene: msg.scene, assembly: msg.assembly } });
    shownDone("wb_update", msg.scene, t0);
}

function shownDone(type: string, scene: string, t0: number) {
    appliedLog.push({ type, scene, ms: performance.now() - t0, t: Date.now() });
    send({ type: "shown", scene });
    refreshPins();
}

export function refreshPins() {
    const { review, shown, item } = getStore();
    if (!shown) return;
    viewer.setPins(review.pins.filter((p) => p.assembly === shown.assembly), item);
}

function handle(msg: Any) {
    switch (msg.type) {
        case "wb_hello":
            setStore({ settings: msg.settings, state: msg, connected: true });
            break;
        case "wb_state":
            setStore({ state: msg });
            break;
        case "wb_review":
            setStore({ review: msg });
            refreshPins();
            break;
        case "wb_scene":
            enqueue(() => showScene(msg));
            break;
        case "wb_update":
            enqueue(() => applyUpdate(msg));
            break;
        case "wb_backend":
            viewer.handleMessage(msg.reply);
            break;
        case "wb_pick":
            setStore({ selection: msg.selection });
            break;
        case "wb_notice":
            notify(msg.level === "error" ? "error" : "info", msg.message);
            break;
    }
}

export function connect() {
    if (!token) {
        notify("error", "No token: open this page with `wbench open`.");
        return;
    }
    ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onmessage = (ev) => {
        const text = ev.data as string;
        const msg = JSON.parse(text);
        received.push({ type: msg.type, bytes: text.length, t: Date.now(), scene: msg.scene });
        handle(msg);
    };
    ws.onclose = () => {
        setStore({ connected: false });
        setTimeout(connect, 1500); // the hub restarts or the network blips: try again
    };
}
