// Spike S5 driver helpers (Node 22 builtins only): the stock ocp_viewer server, the Python host
// and headless Chromium as child process groups that are always stopped, a minimal CDP client,
// and the host's JSON-lines control channel. Adapted from S1's driver/lib.mjs and the scratch
// capture.mjs.

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SPIKE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const RESULTS = path.join(SPIKE, "results");
// S5_SCRATCH: Chromium profiles and the servers' private HOME; S5_SNAPSHOT: the frozen my-robot copy
const SCRATCH_DEFAULT = "/tmp/claude-1000/-home-sandro-work-privat-my-robot/0addebd4-35be-458b-990c-7ebe8c983d5f/scratchpad";
export const SCRATCH = process.env.S5_SCRATCH ?? `${SCRATCH_DEFAULT}/s5`;
export const SNAPSHOT = process.env.S5_SNAPSHOT ?? `${SCRATCH_DEFAULT}/robot-snap-s5`;
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const wall = () => performance.timeOrigin + performance.now();

const children = new Set();

function killGroup(child, signal) {
    try {
        process.kill(-child.pid, signal);
    } catch {
        /* gone */
    }
}

export async function stopChild(child, { first = "SIGTERM", graceMs = 4000 } = {}) {
    if (!child) return;
    if (child.exitCode === null && child.signalCode === null) {
        killGroup(child, first);
        const t0 = Date.now();
        while (child.exitCode === null && child.signalCode === null && Date.now() - t0 < graceMs) await sleep(50);
    }
    killGroup(child, "SIGKILL"); // the whole group: uv's python child, renderer/GPU processes
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

// Every child: stale NVIDIA variables unset, no display (nothing can open a window), no
// VIRTUAL_ENV (uv would use the wrong environment), and a private HOME so the ocp_viewer server's
// port registry (~/.ocpvscode) and settings are the spike's own; uv's cache stays where it is.
export function childEnv(extra = {}) {
    const env = { ...process.env, ...extra };
    for (const k of ["__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR", "VIRTUAL_ENV", "DISPLAY", "WAYLAND_DISPLAY"]) delete env[k];
    return env;
}

function pyEnv() {
    const home = path.join(SCRATCH, "home");
    mkdirSync(home, { recursive: true });
    return childEnv({ HOME: home, UV_CACHE_DIR: "/home/sandro/.cache/uv", PYTHONUNBUFFERED: "1" });
}

export function startServer(port, log) {
    const child = spawn("uv", ["run", "--no-sync", "python", "-m", "ocp_viewer", "--port", String(port)], {
        cwd: SNAPSHOT, env: pyEnv(), stdio: ["ignore", "pipe", "pipe"], detached: true,
    });
    children.add(child);
    const state = { child, lines: [], registered: 0 };
    const onData = (d) => {
        for (const line of String(d).split("\n")) {
            if (!line.trim()) continue;
            state.lines.push({ wall: wall(), line });
            log?.(`[server] ${line}`);
            if (line.includes("Browser as viewer client registered")) state.registered++;
        }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    return state;
}

export async function waitUntil(fn, { timeoutMs = 30000, intervalMs = 50, what = "condition" } = {}) {
    const t0 = Date.now();
    for (;;) {
        const v = await fn();
        if (v) return v;
        if (Date.now() - t0 > timeoutMs) throw new Error(`timeout waiting for ${what}`);
        await sleep(intervalMs);
    }
}

export function startHost(port, log) {
    const child = spawn("uv", ["run", "--no-sync", "python", path.join(SPIKE, "py", "s5host.py"), "--port", String(port)], {
        cwd: SNAPSHOT, env: pyEnv(), stdio: ["pipe", "pipe", "pipe"], detached: true,
    });
    children.add(child);
    let buf = "";
    let next = 1;
    const pending = new Map();
    let ready = false;
    child.stdout.on("data", (d) => {
        buf += String(d);
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, i);
            buf = buf.slice(i + 1);
            if (!line.trim()) continue;
            const msg = JSON.parse(line);
            if (msg.ready) {
                ready = true;
                continue;
            }
            const p = pending.get(msg.id);
            if (!p) continue;
            pending.delete(msg.id);
            if (msg.ok) p.resolve(msg.result);
            else p.reject(new Error(`${msg.error}\n${msg.trace}`));
        }
    });
    child.stderr.on("data", (d) => {
        for (const line of String(d).split("\n")) if (line.trim()) log?.(`[host] ${line}`);
    });
    return {
        child,
        ready: () => waitUntil(() => ready || child.exitCode !== null, { timeoutMs: 120000, what: "host ready" }),
        call(op, args = {}, { timeoutMs = 600000 } = {}) {
            const id = next++;
            child.stdin.write(JSON.stringify({ id, op, args }) + "\n");
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    pending.delete(id);
                    reject(new Error(`host op ${op} timed out`));
                }, timeoutMs);
                pending.set(id, {
                    resolve: (v) => (clearTimeout(timer), resolve(v)),
                    reject: (e) => (clearTimeout(timer), reject(e)),
                });
            });
        },
    };
}

export async function startChrome({ width, height, cdpPort, flags = [], profileDir, log }) {
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
        "--disable-features=MediaRouter,OptimizationHints",
        ...flags,
        "about:blank",
    ];
    const child = spawn("chromium", args, { env: childEnv(), stdio: ["ignore", "pipe", "pipe"], detached: true });
    children.add(child);
    const out = [];
    child.stdout.on("data", (d) => out.push(String(d)));
    child.stderr.on("data", (d) => out.push(String(d)));
    let version = null;
    const t0 = Date.now();
    while (Date.now() - t0 < 20000) {
        try {
            const r = await fetch(`http://127.0.0.1:${cdpPort}/json/version`);
            if (r.ok) {
                version = await r.json();
                break;
            }
        } catch {
            /* not yet */
        }
        if (child.exitCode !== null) throw new Error(`chromium exited: ${out.join("").slice(-2000)}`);
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
    return { child, page, version, out };
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
        } else if (msg.method) for (const fn of listeners) fn(msg);
    };
    const c = {
        send(method, params = {}) {
            const id = next++;
            ws.send(JSON.stringify({ id, method, params }));
            return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
        },
        on(fn) {
            listeners.add(fn);
            return () => listeners.delete(fn);
        },
        waitFor(method, timeoutMs = 30000) {
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => (off(), reject(new Error(`timeout waiting for ${method}`))), timeoutMs);
                const off = c.on((m) => {
                    if (m.method === method) {
                        clearTimeout(timer);
                        off();
                        resolve(m.params);
                    }
                });
            });
        },
        async eval(expression, { timeoutMs = 120000 } = {}) {
            const r = await Promise.race([
                c.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }),
                sleep(timeoutMs).then(() => {
                    throw new Error(`eval timeout: ${expression.slice(0, 100)}`);
                }),
            ]);
            if (r.exceptionDetails) throw new Error(`page exception: ${JSON.stringify(r.exceptionDetails).slice(0, 1500)}`);
            return r.result.value;
        },
        async mouse(type, x, y, extra = {}) {
            await c.send("Input.dispatchMouseEvent", { type, x, y, button: type === "mouseMoved" ? "none" : "left", ...extra });
        },
        async screenshot(file) {
            const r = await c.send("Page.captureScreenshot", { format: "png" });
            writeFileSync(file, Buffer.from(r.data, "base64"));
            return file;
        },
        close() {
            ws.close();
        },
    };
    return c;
}

export function writeJson(name, obj) {
    mkdirSync(RESULTS, { recursive: true });
    const file = path.join(RESULTS, name);
    writeFileSync(file, JSON.stringify(obj, null, 1));
    return file;
}

export function stats(a) {
    const s = [...a].filter((x) => Number.isFinite(x)).sort((x, y) => x - y);
    if (!s.length) return null;
    const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { n: s.length, mean: s.reduce((x, y) => x + y, 0) / s.length, median: q(0.5), p95: q(0.95), p99: q(0.99), max: s[s.length - 1], min: s[0] };
}
