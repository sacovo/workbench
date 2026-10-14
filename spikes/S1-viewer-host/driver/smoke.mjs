// Smoke test: host + headless Chromium + our page + one show of the robot.
import { RESULTS, SPIKE, controlClient, sleep, startChrome, startHost, stopAll, waitUntil } from "./lib.mjs";
import path from "node:path";

const PORT = 8765;
const SWIFTSHADER = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"];
const hostLog = [];
try {
    await startHost(PORT, hostLog);
    const ctl = await controlClient(PORT);
    console.log("ping", await ctl.call("ping"));
    const chromeLog = [];
    const { page, version } = await startChrome({
        url: `http://127.0.0.1:${PORT}/`, width: 1600, height: 1000, cdpPort: 9333, flags: SWIFTSHADER,
        profileDir: path.join(SPIKE, ".chrome-profile"), log: chromeLog,
    });
    console.log("chrome", version.Browser);
    console.log("page", await ctl.call("wait_page", { timeout: 30 }));
    await waitUntil(() => page.eval("!!window.__s1 && !!window.__s1.booted"), { what: "boot" });
    const show = await ctl.call("show_robot", {});
    console.log("show", JSON.stringify(show));
    await waitUntil(() => page.eval("window.__s1.timings.length > 0"), { what: "model timing", timeoutMs: 60000 });
    console.log("timing", JSON.stringify(await page.eval("window.__s1.timings[0]")));
    console.log("gl", JSON.stringify(await page.eval("window.__s1.glInfo()")));
    console.log("scene", JSON.stringify(await page.eval("window.__s1.sceneStats()")));
    const labels = await page.eval("window.__s1.treeLabels()");
    console.log("tree labels", labels.length, labels.slice(0, 8));
    console.log("camera", JSON.stringify(await page.eval("window.__s1.cameraState()")));
    await page.screenshot(path.join(RESULTS, "smoke.png"));
    console.log("console", page.console.slice(0, 20));
    await ctl.call("shutdown");
    await sleep(500);
} catch (e) {
    console.error("SMOKE FAILED", e);
    console.error(hostLog.slice(-30).join("\n"));
} finally {
    await stopAll();
}
