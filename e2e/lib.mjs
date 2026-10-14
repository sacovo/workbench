// Helpers for the end-to-end driver (adapted from spike S1): children in their own process groups,
// killed as groups; headless Chromium on the AMD GPU; a minimal CDP client. Node builtins only.
//
// Nothing here can open a window on the owner's screen: every child gets an environment without
// DISPLAY and WAYLAND_DISPLAY, and Chromium always runs --headless=new.

import { spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const children = new Set();

function killGroup(child, signal = "SIGTERM") {
    try {
        process.kill(-child.pid, signal);
    } catch {
        /* already gone */
    }
}

export async function stopChild(child, { signal = "SIGTERM", graceMs = 8000 } = {}) {
    if (!child) return;
    if (child.exitCode === null && child.signalCode === null) {
        killGroup(child, signal);
        const t0 = Date.now();
        while (child.exitCode === null && child.signalCode === null && Date.now() - t0 < graceMs) await sleep(50);
    }
    killGroup(child, "SIGKILL"); // the group: renderer, GPU and utility processes, or anything left
    children.delete(child);
}

export async function stopAll() {
    for (const c of [...children]) await stopChild(c, { graceMs: 3000 });
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

// The stale NVIDIA variables unset (plan, owner actions), no foreign virtual environment, no display.
export function childEnv(extra = {}) {
    const env = { ...process.env };
    for (const k of ["__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR", "VIRTUAL_ENV", "DISPLAY", "WAYLAND_DISPLAY"]) delete env[k];
    return { ...env, ...extra };
}

export function startProcess(command, args, { cwd, log = [], env = childEnv() } = {}) {
    const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], detached: true });
    children.add(child);
    const onData = (d) => {
        for (const line of String(d).split("\n")) if (line.trim()) log.push(line);
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    return child;
}

// Headless Chromium on the AMD GPU through ANGLE on Vulkan (spike S1's probe).
export const GPU_FLAGS = ["--use-angle=vulkan", "--enable-features=Vulkan", "--ignore-gpu-blocklist"];

export async function startChrome({ url, width, height, cdpPort, profileDir, log = [] }) {
    rmSync(profileDir, { recursive: true, force: true });
    mkdirSync(profileDir, { recursive: true });
    const args = [
        "--headless=new",
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
        ...GPU_FLAGS,
        "about:blank",
    ];
    const child = startProcess("chromium", args, { log });
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
        if (child.exitCode !== null) throw new Error(`chromium exited early: ${log.join("\n").slice(-2000)}`);
        await sleep(100);
    }
    if (!version) throw new Error("CDP did not come up");
    const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
    const page = await cdpConnect(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    page.console = [];
    page.on((m) => {
        if (m.method === "Runtime.consoleAPICalled" && (m.params.type === "error" || m.params.type === "warning")) {
            page.console.push(`[${m.params.type}] ${(m.params.args || []).map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 400)}`);
        } else if (m.method === "Runtime.exceptionThrown") {
            page.console.push(`[exception] ${JSON.stringify(m.params.exceptionDetails).slice(0, 800)}`);
        }
    });
    if (url) {
        const loaded = page.waitFor("Page.loadEventFired", 30000);
        await page.send("Page.navigate", { url });
        await loaded;
    }
    return { child, page, version };
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
        async click(x, y, { clickCount = 1 } = {}) {
            await client.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount });
            await client.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount });
        },
        async key(key, code, keyCode) {
            await client.send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: keyCode });
            await client.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: keyCode });
        },
        async screenshot() {
            return Buffer.from((await client.send("Page.captureScreenshot", { format: "png" })).data, "base64");
        },
        close() {
            ws.close();
        },
    };
    return client;
}

export async function waitUntil(fn, { timeoutMs = 30000, intervalMs = 50, what = "condition" } = {}) {
    const t0 = Date.now();
    for (;;) {
        const v = await fn();
        if (v) return v;
        if (Date.now() - t0 > timeoutMs) throw new Error(`timeout after ${timeoutMs} ms waiting for ${what}`);
        await sleep(intervalMs);
    }
}
