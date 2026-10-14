// Which headless Chromium flag sets give WebGL on the real GPU? For each: launch headless (no
// display variables), create a WebGL2 context on a blank page, read the unmasked renderer and the
// browser's SystemInfo. Writes results/gpu_probe.json.
import path from "node:path";
import { SPIKE, sleep, startChrome, stopChild, writeJson } from "./lib.mjs";

export const FLAG_SETS = {
    swiftshader: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    default_headless: [],
    angle_vulkan: ["--use-angle=vulkan", "--enable-features=Vulkan", "--ignore-gpu-blocklist"],
    angle_vulkan_nosurface: ["--use-angle=vulkan", "--enable-features=Vulkan", "--disable-vulkan-surface", "--ignore-gpu-blocklist"],
    angle_gl_egl: ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist"],
    angle_gl: ["--use-gl=angle", "--use-angle=gl", "--ignore-gpu-blocklist"],
    use_gl_egl: ["--use-gl=egl", "--ignore-gpu-blocklist"],
    ozone_headless_vulkan: ["--ozone-platform=headless", "--use-angle=vulkan", "--enable-features=Vulkan", "--ignore-gpu-blocklist"],
    ozone_headless_gl_egl: ["--ozone-platform=headless", "--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist"],
};

const PROBE = `(() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl2');
  if (!gl) return { webgl2: false };
  const d = gl.getExtension('WEBGL_debug_renderer_info');
  return { webgl2: true, vendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : null,
           renderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
           version: gl.getParameter(gl.VERSION) };
})()`;

export async function probe(name, flags, cdpPort = 9340) {
    const log = [];
    const out = { name, flags };
    let child;
    try {
        const r = await startChrome({ width: 800, height: 600, cdpPort, flags, profileDir: path.join(SPIKE, `.chrome-probe-${name}`), log });
        child = r.child;
        await sleep(500);
        out.webgl = await r.page.eval(PROBE);
        const info = await r.browser.send("SystemInfo.getInfo");
        out.gpu = {
            devices: info.gpu.devices.map((d) => ({ vendorId: d.vendorId, deviceId: d.deviceId, vendorString: d.vendorString, deviceString: d.deviceString, driverVendor: d.driverVendor, driverVersion: d.driverVersion })),
            featureStatus: Object.fromEntries(Object.entries(info.gpu.featureStatus).filter(([k]) => /webgl|gpu_compositing|rasterization|vulkan|skia/i.test(k))),
            driverBugWorkarounds: info.gpu.driverBugWorkarounds.length,
            auxAttributes: Object.fromEntries(Object.entries(info.gpu.auxAttributes ?? {}).filter(([k]) => /gl|angle|vulkan|display|sandbox|initial/i.test(k))),
        };
        out.hardware = !!(out.webgl?.renderer && /AMD|Radeon|RADV|radeonsi/i.test(out.webgl.renderer));
    } catch (e) {
        out.error = String(e.message ?? e).slice(0, 1500);
    } finally {
        if (child) await stopChild(child);
        out.stderrTail = log.join("").split("\n").filter((l) => /ERROR|WARNING|vulkan|egl|gpu/i.test(l)).slice(-12);
    }
    return out;
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const results = [];
    for (const [name, flags] of Object.entries(FLAG_SETS)) {
        const r = await probe(name, flags);
        console.log(name, "=>", r.webgl?.renderer ?? r.error, "hardware:", r.hardware);
        results.push(r);
    }
    console.log("wrote", writeJson("gpu_probe.json", { when: new Date().toISOString(), results }));
}
