// Shared helpers for the S1 drivers: start/stop the Python host and headless Chromium (each in
// its own process group, killed as a group), a minimal CDP client, and the host's control channel.
// Node 22 builtins only (global WebSocket and fetch).

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SPIKE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const RESULTS = path.join(SPIKE, "results");
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const children = new Set();

function killGroup(child, signal = "SIGTERM") {
    try {
        process.kill(-child.pid, signal);
    } catch {
        /* already gone */
    }
}

export async function stopChild(child, graceMs = 3000) {
    if (!child || child.exitCode !== null || child.signalCode !== null) {
        children.delete(child);
        return;
    }
    killGroup(child, "SIGTERM");
    const t0 = Date.now();
    while (child.exitCode === null && child.signalCode === null && Date.now() - t0 < graceMs) await sleep(50);
    killGroup(child, "SIGKILL"); // the group: renderer, GPU and utility processes too
    children.delete(child);
}

export async function stopAll() {
    for (const c of [...children]) await stopChild(c);
}

for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, async () => {
        await stopAll();
        process.exit(130);
    });
}
process.on("exit", () => {
    for (const c of children) killGroup(c, "SIGKILL");
});

// Environment for every child: the stale NVIDIA variables unset, and no display, so nothing can
// ever open a window on the owner's screen (headless Chromium needs none).
export function childEnv(extra = {}) {
    const env = { ...process.env };
    for (const k of ["__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR", "VIRTUAL_ENV", "DISPLAY", "WAYLAND_DISPLAY"]) delete env[k];
    return { ...env, ...extra };
}

export async function startHost(port, log = []) {
    const child = spawn("uv", ["run", "python", "host/s1host.py", "--port", String(port)], {
        cwd: SPIKE,
        env: childEnv(),
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
    });
    children.add(child);
    let ready = false;
    const onData = (d) => {
        for (const line of String(d).split("\n")) {
            if (!line.trim()) continue;
            log.push(line);
            if (line.startsWith("READY")) ready = true;
        }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    const t0 = Date.now();
    while (!ready) {
        if (child.exitCode !== null) throw new Error(`host exited: ${log.slice(-20).join("\n")}`);
        if (Date.now() - t0 > 120000) throw new Error("host did not become ready");
        await sleep(100);
    }
    return child;
}

export async function controlClient(port) {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/`);
    await new Promise((res, rej) => {
        ws.onopen = res;
        ws.onerror = rej;
    });
    ws.send(JSON.stringify({ role: "control" }));
    let next = 1;
    const pending = new Map();
    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        const p = pending.get(msg.id);
        if (!p) return;
        pending.delete(msg.id);
        if (msg.ok) p.resolve(msg.result);
        else p.reject(new Error(`${msg.error}\n${msg.trace ?? ""}`));
    };
    return {
        call(op, args = {}) {
            const id = next++;
            ws.send(JSON.stringify({ id, op, args }));
            return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
        },
        close() {
            ws.close();
        },
    };
}

export async function startChrome({ url, width, height, cdpPort, flags = [], profileDir, log = [], visible = false }) {
    rmSync(profileDir, { recursive: true, force: true });
    mkdirSync(profileDir, { recursive: true });
    const args = [
        // visible: a native Wayland app window (only ever started by the owner's script)
        ...(visible ? ["--ozone-platform=wayland"] : ["--headless=new"]),
        `--remote-debugging-port=${cdpPort}`,
        "--remote-allow-origins=*",
        `--user-data-dir=${profileDir}`,
        `--window-size=${width},${height}`,
        "--force-device-scale-factor=1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-features=MediaRouter,OptimizationHints",
        ...flags,
        visible ? `--app=${url}` : "about:blank",
    ];
    const env = visible ? childEnv({ WAYLAND_DISPLAY: process.env.WAYLAND_DISPLAY }) : childEnv();
    if (visible && !env.WAYLAND_DISPLAY) throw new Error("visible mode needs WAYLAND_DISPLAY");
    const child = spawn("chromium", args, { env, stdio: ["ignore", "pipe", "pipe"], detached: true });
    children.add(child);
    child.stdout.on("data", (d) => log.push(String(d)));
    child.stderr.on("data", (d) => log.push(String(d)));
    const t0 = Date.now();
    let version = null;
    while (Date.now() - t0 < 20000) {
        try {
            const r = await fetch(`http://127.0.0.1:${cdpPort}/json/version`);
            if (r.ok) {
                version = await r.json();
                break;
            }
        } catch {
            /* not up yet */
        }
        if (child.exitCode !== null) throw new Error(`chromium exited early: ${log.join("").slice(-2000)}`);
        await sleep(100);
    }
    if (!version) throw new Error("CDP did not come up");
    const browser = await cdpConnect(version.webSocketDebuggerUrl);
    const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
    const pageTarget = targets.find((t) => t.type === "page");
    const page = await cdpConnect(pageTarget.webSocketDebuggerUrl);
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    page.console = [];
    page.on((m) => {
        if (m.method === "Runtime.consoleAPICalled") {
            const text = (m.params.args || []).map((a) => a.value ?? a.description ?? "").join(" ");
            if (m.params.type === "error" || m.params.type === "warning") page.console.push(`[${m.params.type}] ${text.slice(0, 400)}`);
        } else if (m.method === "Runtime.exceptionThrown") {
            page.console.push(`[exception] ${JSON.stringify(m.params.exceptionDetails).slice(0, 800)}`);
        }
    });
    if (url && !visible) await navigate(page, url); // an --app window has opened the url itself
    return { child, browser, page, version };
}

export async function navigate(page, url) {
    const loaded = page.waitFor("Page.loadEventFired", 30000);
    await page.send("Page.navigate", { url });
    await loaded;
}

export async function cdpConnect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
        ws.onopen = res;
        ws.onerror = rej;
    });
    let next = 1;
    const pending = new Map();
    const listeners = new Set();
    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id !== undefined && pending.has(msg.id)) {
            const p = pending.get(msg.id);
            pending.delete(msg.id);
            if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
            else p.resolve(msg.result);
        } else if (msg.method) {
            for (const fn of listeners) fn(msg);
        }
    };
    const client = {
        send(method, params = {}) {
            const id = next++;
            ws.send(JSON.stringify({ id, method, params }));
            return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
        },
        on(fn) {
            listeners.add(fn);
            return () => listeners.delete(fn);
        },
        waitFor(method, timeoutMs = 10000) {
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    off();
                    reject(new Error(`timeout waiting for ${method}`));
                }, timeoutMs);
                const off = client.on((m) => {
                    if (m.method === method) {
                        clearTimeout(timer);
                        off();
                        resolve(m.params);
                    }
                });
            });
        },
        async eval(expression, { timeoutMs = 120000 } = {}) {
            // the timeout timer is cleared afterwards: a pending timer keeps Node alive after the run
            let timer;
            const timeout = new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error(`eval timeout: ${expression.slice(0, 80)}`)), timeoutMs);
            });
            let r;
            try {
                r = await Promise.race([client.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }), timeout]);
            } finally {
                clearTimeout(timer);
            }
            if (r.exceptionDetails) throw new Error(`page exception: ${JSON.stringify(r.exceptionDetails).slice(0, 1500)}`);
            return r.result.value;
        },
        async move(x, y) {
            await client.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
        },
        async click(x, y, { moveFirst = true, settleMs = 120, clickCount = 1 } = {}) {
            if (moveFirst) {
                await client.move(x - 3, y - 3);
                await client.move(x, y);
                await sleep(settleMs);
            }
            await client.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount });
            await client.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount });
        },
        async key(key, code, keyCode) {
            await client.send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: keyCode });
            await client.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: keyCode });
        },
        async screenshot(file) {
            const r = await client.send("Page.captureScreenshot", { format: "png" });
            writeFileSync(file, Buffer.from(r.data, "base64"));
            return file;
        },
        close() {
            ws.close();
        },
    };
    return client;
}

export async function waitUntil(fn, { timeoutMs = 30000, intervalMs = 100, what = "condition" } = {}) {
    const t0 = Date.now();
    for (;;) {
        const v = await fn();
        if (v) return v;
        if (Date.now() - t0 > timeoutMs) throw new Error(`timeout waiting for ${what}`);
        await sleep(intervalMs);
    }
}

export function writeJson(name, obj) {
    mkdirSync(RESULTS, { recursive: true });
    const file = path.join(RESULTS, name);
    writeFileSync(file, JSON.stringify(obj, null, 2));
    return file;
}

export function stats(a) {
    const s = [...a].filter((x) => Number.isFinite(x)).sort((x, y) => x - y);
    if (!s.length) return null;
    const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { n: s.length, mean: s.reduce((x, y) => x + y, 0) / s.length, median: q(0.5), p95: q(0.95), max: s[s.length - 1] };
}
