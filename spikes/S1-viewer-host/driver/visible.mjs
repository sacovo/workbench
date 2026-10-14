// For the owner: the robot in a visible Chromium app window (native Wayland, real GPU), shown through
// our page and host, then a scripted orbit timed in the page. Prints a summary and writes
// results/visible_<angle>_<W>x<H>_<time>.json. Closes the window and the host at the end.
//
//   ./run_visible.sh                          1600x1000, Chromium's default GL path
//   ./run_visible.sh --size 2560x1440
//   ./run_visible.sh --angle vulkan           ANGLE on Vulkan (RADV)
//   ./run_visible.sh --angle gl-egl           ANGLE on GL via EGL (radeonsi)
//
// Keep the window visible on the current workspace while it runs (about 20 s): Chromium throttles
// animation frames of hidden or occluded windows, which would read as a slow GPU.

import path from "node:path";
import { SPIKE, controlClient, startChrome, startHost, stopAll, waitUntil, writeJson } from "./lib.mjs";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
const [W, H] = (args.size ?? "1600x1000").split("x").map(Number);
const ANGLE = args.angle ?? "default";
const HEADLESS = args.headless === "1"; // only to test this script without a window
const FLAGS = {
    default: [],
    vulkan: ["--use-angle=vulkan", "--enable-features=Vulkan", "--ignore-gpu-blocklist"],
    "gl-egl": ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist"],
}[ANGLE];
if (!FLAGS) throw new Error(`--angle must be default, vulkan or gl-egl, not ${ANGLE}`);
const PORT = Number(args.port ?? 8790);

const result = { when: new Date().toISOString(), angle: ANGLE, flags: FLAGS, window: [W, H], visible: !HEADLESS };
const hostLog = [];
try {
    await startHost(PORT, hostLog);
    const ctl = await controlClient(PORT);
    const { page: p } = await startChrome({ url: `http://127.0.0.1:${PORT}/`, width: W, height: H, cdpPort: Number(args.cdp ?? 9350), flags: FLAGS,
        profileDir: path.join(SPIKE, ".chrome-profile-visible"), visible: !HEADLESS });
    await ctl.call("wait_page", { timeout: 60 });
    await waitUntil(() => p.eval("!!window.__s1 && !!window.__s1.booted"), { timeoutMs: 60000, what: "page boot" });
    const host = await ctl.call("show_robot", {});
    const shown = await waitUntil(() => p.eval("__s1.timings[0] ?? null"), { timeoutMs: 120000, what: "first frame" });
    result.gl = await p.eval("__s1.glInfo()");
    result.scene = await p.eval("__s1.sceneStats()");
    result.showToFirstFrameMs = shown.firstFrameWallMs - host.show_start_wall_ms;
    result.host = { tessellate_convert_s: host.tessellate_convert_s, payload_bytes: host.payload_bytes };
    result.page = shown;
    await p.eval("__s1.orbitTo(-60, 35, 1.0)");
    result.orbit = await p.eval("__s1.bench({ frames: 300, degPerFrame: 1.2, sync: false })", { timeoutMs: 600000 });
    result.orbitSync = await p.eval("__s1.bench({ frames: 120, degPerFrame: 3, sync: true })", { timeoutMs: 600000 });
    const iv = result.orbit.interval;
    result.verdict60fps = iv && iv.p95 <= 17.5 ? "pass" : "fail";
    console.log(`renderer          ${result.gl.renderer}`);
    console.log(`canvas            ${result.orbit.canvas.join(" x ")} px`);
    console.log(`show to frame     ${result.showToFirstFrameMs.toFixed(0)} ms (tessellate ${host.tessellate_convert_s.toFixed(2)} s, ${(host.payload_bytes / 1e6).toFixed(1)} MB)`);
    console.log(`orbit, vsync      median ${iv.median.toFixed(2)} ms, p95 ${iv.p95.toFixed(2)} ms, max ${iv.max.toFixed(1)} ms, ${result.orbit.fpsMean.toFixed(1)} fps mean, ${result.orbit.over16_7ms} frames over 25 ms`);
    console.log(`frame cost, sync  median ${result.orbitSync.updateCpu.median.toFixed(2)} ms, p95 ${result.orbitSync.updateCpu.p95.toFixed(2)} ms (render + GPU finish per frame)`);
    console.log(`60 fps (p95 frame interval <= 17.5 ms): ${result.verdict60fps}`);
    await ctl.call("shutdown").catch(() => {});
} catch (e) {
    result.error = String(e.stack ?? e);
    console.error("FAILED", e, hostLog.slice(-20).join("\n"));
} finally {
    const file = writeJson(`visible_${ANGLE}_${W}x${H}_${Date.now()}.json`, result);
    console.log("wrote", file);
    await stopAll();
}
