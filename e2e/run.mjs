// End to end through the real pieces: `wbench serve` on a project (its real kernel in the project's
// environment), our page in headless Chromium on the GPU, picks clicked like a user clicks them,
// the `wbench` CLI as the agent uses it. Each step is timed; results go to <out>/e2e_<target>.json.
//
//   node e2e/run.mjs --target minimal  --out <dir>
//   node e2e/run.mjs --target my-robot --out <dir>
//
// Checks: the page loads and shows the scene; a face, an edge and a vertex of a known part, clicked
// with the stock Properties tool, come back from `wbench selection` as that part and that kind, with
// geometry that agrees with what the page drew; an `ask` made with the CLI appears in the inbox;
// answering in the page appends an answer event to the item's file; `wbench show` with new arguments
// reaches the page as an incremental update; killing the kernel shows the crash banner while the
// scene stays. Review items it creates are deleted at the end; it refuses to run on a project that
// already has review/.

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { childEnv, sleep, startChrome, startProcess, stopAll, stopChild, waitUntil } from "./lib.mjs";

const run = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const WB = path.resolve(HERE, "../.venv/bin/wbench");
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));

const TARGETS = {
    // extras: also the Comment and Views tabs (kept off the robot run to keep it short)
    minimal: { project: path.resolve(HERE, "../examples/minimal"), assembly: "minimal", part: "plate", update: { pin_height: 12 }, extras: true },
    "my-robot": { project: "/home/sandro/work/privat/my-robot", assembly: "robot", part: "lower_plate", update: { height: 500 } },
};
const NAME = args.target ?? "minimal";
const T = TARGETS[NAME];
if (!T) throw new Error(`--target must be one of ${Object.keys(TARGETS)}`);
const OUT = path.resolve(args.out ?? path.join(os.tmpdir(), "wb-e2e")); // results and Chromium profiles, outside the repository
const [W, H] = (args.size ?? "1600x1000").split("x").map(Number);
const CDP = Number(args.cdp ?? 9341);
const QUESTIONS = [
    { id: "q1", text: "Which front limit?", options: [{ id: "a", label: "35 mm" }, { id: "b", label: "75 mm" }], recommended: "a" },
    { id: "q2", text: "Anything else?", options: [], free_text: true },
];

mkdirSync(OUT, { recursive: true });
const result = { target: NAME, project: T.project, started: new Date().toISOString(), steps: [], pass: false };
const save = () => writeFileSync(path.join(OUT, `e2e_${NAME}.json`), JSON.stringify(result, null, 2));
const hubLog = [];
const chromeLog = [];
let hub = null;
let chrome = null;
let page = null;
let info = null; // hub.json

async function step(name, fn, { required = true } = {}) {
    const t0 = Date.now();
    process.stdout.write(`${name} ... `);
    const rec = { name };
    try {
        Object.assign(rec, await fn());
        rec.ok = rec.ok ?? true;
    } catch (e) {
        rec.ok = false;
        rec.error = String(e.stack ?? e).slice(0, 3000);
    }
    rec.ms = Date.now() - t0;
    result.steps.push(rec);
    const why = rec.error ? rec.error.split("\n")[0] : JSON.stringify(rec).slice(0, 400);
    console.log(`${rec.ok ? "ok" : "FAILED"} (${rec.ms} ms)${rec.ok ? "" : `\n  ${why}`}`);
    save();
    if (!rec.ok && required) throw new Error(`step ${name} failed`);
    return rec;
}

async function wb(...argv) {
    const t0 = Date.now();
    try {
        const { stdout } = await run(WB, ["-C", T.project, ...argv, "--json"], { env: childEnv(), timeout: 900000, maxBuffer: 64 << 20 });
        return { json: JSON.parse(stdout), ms: Date.now() - t0 };
    } catch (e) {
        throw new Error(`wbench ${argv[0]} failed: ${e.stderr ?? e}`);
    }
}

async function api(pathname) {
    const r = await fetch(`http://127.0.0.1:${info.port}${pathname}`, { headers: { "X-Workbench-Token": info.token } });
    if (!r.ok) throw new Error(`${pathname}: ${r.status} ${await r.text()}`);
    return r.json();
}

function kernelProcesses() {
    const out = [];
    for (const pid of readdirSync("/proc").filter((p) => /^\d+$/.test(p))) {
        try {
            const words = readFileSync(`/proc/${pid}/cmdline`).toString().split("\0");
            if (words.some((w) => w.endsWith("workbench_kernel")) && words.includes(T.project)) out.push(Number(pid));
        } catch {
            /* gone */
        }
    }
    return out;
}

const ev = (expr) => page.eval(expr);

async function frames(n = 2) {
    for (let i = 0; i < n; i++) await ev("new Promise((r) => requestAnimationFrame(() => { __wb.render(); requestAnimationFrame(() => r(true)); }))");
}

async function clickAt(x, y, opts) {
    await page.move(x - 4, y - 3);
    await page.move(x, y);
    await sleep(80);
    await frames(2); // the hover pick happens inside a render
    await page.click(x, y, opts);
}

async function clickSelector(selector) {
    const r = await ev(`__wb.rectOf(${JSON.stringify(selector)})`);
    if (!r || !r.visible) throw new Error(`${selector} is not visible`);
    await page.move(r.x, r.y);
    await page.click(r.x, r.y);
    return r;
}

const sub = (a, b) => a.map((x, i) => x - b[i]);
const dot = (a, b) => a.reduce((s, x, i) => s + x * b[i], 0);
const norm = (a) => Math.hypot(...a);
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

function segDist2d(p, a, b) {
    const d = [b[0] - a[0], b[1] - a[1]];
    const len2 = d[0] * d[0] + d[1] * d[1];
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / len2));
    return Math.hypot(p[0] - (a[0] + t * d[0]), p[1] - (a[1] + t * d[1]));
}

/** Click a target with the Properties tool active and return what `wbench selection` says. */
async function pickAndSelect(target) {
    const seq0 = await ev("__wb.store().selection?.seq ?? 0");
    const t0 = Date.now();
    await clickAt(target.x, target.y);
    await waitUntil(() => ev(`(__wb.store().selection?.seq ?? 0) > ${seq0}`), { timeoutMs: 60000, what: "the pick to come back from the hub" });
    const pageMs = Date.now() - t0;
    const sel = await wb("selection");
    await page.key("Escape", "Escape", 27);
    await frames(1);
    return { sel: sel.json, clickToPickMs: pageMs, selectionCliMs: sel.ms };
}

try {
    if (existsSync(path.join(T.project, "review"))) throw new Error(`${T.project}/review exists; this driver only runs where it can delete what it creates`);
    if (existsSync(path.join(T.project, ".workbench", "hub.json"))) throw new Error(`${T.project}/.workbench/hub.json exists: is a hub running there?`);

    await step("serve: hub up, kernel ready", async () => {
        hub = startProcess(WB, ["-C", T.project, "serve"], { log: hubLog });
        info = await waitUntil(() => {
            try {
                return JSON.parse(readFileSync(path.join(T.project, ".workbench", "hub.json"), "utf8"));
            } catch {
                return null;
            }
        }, { timeoutMs: 30000, what: "hub.json" });
        const state = await waitUntil(async () => {
            let s;
            try {
                s = await api("/api/state");
            } catch {
                return null; // not listening yet
            }
            if (s.kernel.state === "crashed") throw new Error(`the kernel crashed: ${JSON.stringify(s.kernel.error)}`);
            return s.kernel.state === "ready" ? s : null;
        }, { timeoutMs: 300000, what: "the kernel" });
        return { port: info.port, kernel: { generation: state.kernel.generation, pid: state.kernel.pid }, assemblies: Object.keys(state.manifest.assemblies) };
    });

    await step("page: loads and shows the scene", async () => {
        chrome = await startChrome({ url: `http://127.0.0.1:${info.port}/#token=${info.token}`, width: W, height: H, cdpPort: CDP, profileDir: path.join(OUT, `.chrome-${NAME}`), log: chromeLog });
        page = chrome.page;
        const shown = await waitUntil(() => ev("window.__wb && __wb.store().shown"), { timeoutMs: 300000, what: "the scene in the page" });
        await frames(2);
        const state = await api("/api/state");
        const leaves = await ev("__wb.leafCount()");
        const gl = await ev("__wb.glInfo()");
        writeFileSync(path.join(OUT, `e2e_${NAME}_loaded.png`), await page.screenshot());
        const applied = await ev("__wb.appliedLog[0]");
        return {
            ok: shown.assembly === T.assembly && state.scene.scene === shown.scene && leaves >= state.scene.parts,
            scene: shown.scene, assembly: shown.assembly, parts: state.scene.parts, leaves, renderer: gl.renderer,
            blobBytes: state.scene.blob.bytes, pageApplyMs: applied?.ms, tokenInUrl: (await ev("location.href")).includes("token"),
        };
    });

    // Picks: only the part on screen, the Properties tool active, aimed from above at an angle.
    await ev(`__wb.isolate(${JSON.stringify(T.part)})`);
    await ev(`__wb.frame(${JSON.stringify(T.part)}, -60, 50, 0.7)`);
    await frames(2);
    await clickSelector(".tcv_button_properties");
    await frames(2);

    await step(`face pick on ${T.part}`, async () => {
        const target = await ev(`__wb.findFaceTarget(${JSON.stringify(T.part)})`);
        if (!target) throw new Error("no visible face pixel found");
        const { sel, ...timing } = await pickAndSelect(target);
        const w = sel.world;
        let onFace = null;
        if (w.geom_type === "plane") onFace = Math.abs(dot(w.normal, sub(target.point, w.centre)));
        else if (w.axis) onFace = Math.abs(norm(cross(sub(target.point, w.axis.origin), w.axis.direction)) - w.radius);
        const parallel = w.normal && target.normal ? Math.abs(dot(w.normal, target.normal)) : null;
        return {
            ok: sel.label === T.part && sel.kind === "face" && onFace !== null && onFace < 0.05 && target.edgeDistancePx >= 8,
            label: sel.label, kind: sel.kind, geom_type: w.geom_type, area: w.area, edgeDistancePx: target.edgeDistancePx,
            clickedPointToResolvedSurfaceMm: onFace, normalAgreement: parallel, storedPoint: sel.point, ...timing,
        };
    }, { required: false });

    // Edges and vertices of a thin plate have twins a few pixels away (its other face): zoom in on
    // one first, to ZOOM px per mm, so the picker's few-pixel window holds only the one aimed at.
    const ZOOM = 8;
    await step(`edge pick on ${T.part}`, async () => {
        await ev(`__wb.frame(${JSON.stringify(T.part)}, -60, 50, 0.7)`);
        const overview = await ev(`__wb.findEdgeTarget(${JSON.stringify(T.part)}, false)`);
        if (!overview) throw new Error("no visible edge");
        await ev(`__wb.focus(${JSON.stringify(overview.mid)}, ${ZOOM})`);
        await frames(2);
        const target = await ev(`__wb.findEdgeTarget(${JSON.stringify(T.part)})`);
        if (!target) throw new Error("no clear visible edge found");
        const { sel, ...timing } = await pickAndSelect(target);
        const w = sel.world;
        if (sel.kind !== "edge") return { ok: false, label: sel.label, kind: sel.kind, world: w, target, ...timing };
        const ends = Math.min(
            Math.max(norm(sub(w.start, target.start)), norm(sub(w.end, target.end))),
            Math.max(norm(sub(w.start, target.end)), norm(sub(w.end, target.start))),
        );
        const [ps, pe] = await ev(`__wb.project(${JSON.stringify([w.start, w.end])})`);
        const clickToEdgePx = w.geom_type === "line" ? segDist2d([target.x, target.y], ps, pe) : null;
        return {
            ok: sel.label === T.part && sel.kind === "edge" && ends < 0.05 && (clickToEdgePx === null || clickToEdgePx < 3),
            label: sel.label, kind: sel.kind, geom_type: w.geom_type, length: w.length,
            resolvedEndsToDrawnEndsMm: ends, clickToResolvedEdgePx: clickToEdgePx, lengthPx: target.lengthPx,
            vertexPx: target.vertexPx, otherEdgePx: target.otherEdgePx, ...timing,
        };
    }, { required: false });

    await step(`vertex pick on ${T.part}`, async () => {
        await ev(`__wb.frame(${JSON.stringify(T.part)}, -60, 50, 0.7)`);
        const overview = await ev(`__wb.findVertexTarget(${JSON.stringify(T.part)})`);
        if (!overview) throw new Error("no visible vertex");
        await ev(`__wb.focus(${JSON.stringify(overview.point)}, ${ZOOM})`);
        await frames(2);
        const target = await ev(`__wb.findVertexTarget(${JSON.stringify(T.part)}, 12)`);
        if (!target) throw new Error("no visible vertex clear of the others by 12 px");
        const { sel, ...timing } = await pickAndSelect(target);
        const d = norm(sub(sel.world.point, target.point));
        const [pp] = await ev(`__wb.project(${JSON.stringify([sel.world.point])})`);
        const px = Math.hypot(pp[0] - target.x, pp[1] - target.y);
        return {
            ok: sel.label === T.part && sel.kind === "vertex" && d < 0.05 && px < 3,
            label: sel.label, kind: sel.kind, resolvedToDrawnVertexMm: d, clickToResolvedVertexPx: px, otherVertexPx: target.otherVertexPx,
            storedPointIsExact: norm(sub(sel.point, sel.world.point)) === 0, ...timing,
        };
    }, { required: false });

    await clickSelector(".tcv_button_properties"); // tool off
    await ev("__wb.isolate(null)");
    await frames(1);

    let itemId = null;
    await step("ask with the CLI appears in the inbox", async () => {
        await clickSelector('[data-tab="inbox"]');
        const asked = await wb("ask", "E2E check: front limit", "--questions", JSON.stringify(QUESTIONS), "--body", "Asked by the end-to-end driver.");
        itemId = asked.json.id;
        const t0 = Date.now();
        await waitUntil(() => ev(`!!document.querySelector('[data-group="waiting_owner"] [data-item="${itemId}"]')`), { timeoutMs: 10000, what: "the item in the inbox" });
        return { item: itemId, askCliMs: asked.ms, cliReturnToInboxMs: Date.now() - t0 };
    }, { required: false });

    if (itemId) {
        await step("answer in the page appends an answer event", async () => {
            await clickSelector(`[data-item="${itemId}"]`);
            await waitUntil(() => ev(`!!document.querySelector('[data-item-view="${itemId}"] [data-question="q1"]')`), { timeoutMs: 10000, what: "the item view" });
            await clickSelector(`[data-question="q1"] [data-option="b"]`);
            const t0 = Date.now();
            await clickSelector('[data-action="submit-answer"]');
            const file = path.join(T.project, "review", `${itemId}.yaml`);
            const text = await waitUntil(() => {
                const t = readFileSync(file, "utf8");
                return t.includes("event: answer") ? t : null;
            }, { timeoutMs: 10000, what: "an answer event in the file" });
            const answer = text.slice(text.lastIndexOf("event: answer"));
            const inboxed = await waitUntil(() => ev(`!!document.querySelector('[data-group="waiting_agent"] [data-item="${itemId}"]') || !!document.querySelector('[data-item-view="${itemId}"] .answered')`), { timeoutMs: 10000, what: "the page to show the answer" });
            return {
                ok: /by: \{role: owner\}/.test(answer) && /q1: \{choice: b\}/.test(answer) && !/q2:/.test(answer),
                submitToFileMs: Date.now() - t0, pageShowsIt: inboxed, event: answer.slice(0, 400),
            };
        }, { required: false });
    }

    if (T.extras) {
        await step("comment from the page: frozen evidence, thumbnail and a pin", async () => {
            await ev(`__wb.isolate(${JSON.stringify(T.part)})`);
            await ev(`__wb.frame(${JSON.stringify(T.part)}, -60, 50, 0.7)`);
            await frames(2);
            await clickSelector(".tcv_button_properties");
            await frames(2);
            const target = await ev(`__wb.findFaceTarget(${JSON.stringify(T.part)})`);
            const { sel } = await pickAndSelect(target);
            await clickSelector(".tcv_button_properties"); // tool off
            await clickSelector('[data-tab="comment"]');
            await waitUntil(() => ev(`document.querySelector('[data-pick-label]')?.textContent === ${JSON.stringify(T.part)} && !document.querySelector('[data-action="send-comment"]').disabled`),
                { timeoutMs: 10000, what: "the pick in the Comment tab, ready to send" });
            const t0 = Date.now();
            await clickSelector('[data-action="send-comment"]');
            const id = await waitUntil(() => ev("document.querySelector('[data-item-view]')?.getAttribute('data-item-view') ?? null"), { timeoutMs: 15000, what: "the new item" })
                .catch(async (e) => {
                    throw new Error(`${e.message}; page notices: ${JSON.stringify(await ev("__wb.store().notices"))}`);
                });
            const assets = path.join(T.project, "review", "assets", id);
            const evidence = JSON.parse(readFileSync(path.join(assets, "evidence.json"), "utf8"));
            const png = readFileSync(path.join(assets, "thumbnail.png"));
            const pinned = await waitUntil(() => ev(`__wb.overlay.headCentre(${JSON.stringify(id)})`), { timeoutMs: 10000, what: "the pin" });
            const item = (await wb("item", id)).json;
            return {
                ok: item.kind === "comment" && item.targets[0].label === T.part && evidence.part === T.part && norm(sub(evidence.point, sel.point)) === 0
                    && png.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) && png.length > 1000 && !!pinned,
                item: id, sendToItemMs: Date.now() - t0, thumbnailBytes: png.length, evidencePoint: evidence.point, evidenceBuild: evidence.build,
                camera: evidence.camera, pinHead: pinned, attachments: item.attachments,
            };
        }, { required: false });

        await step("save a view, move away, apply it again", async () => {
            await clickSelector('[data-tab="views"]');
            const saved = await ev("__wb.cameraState()");
            await clickSelector('[data-action="save-view"]');
            const id = await waitUntil(() => ev("document.querySelector('[data-view]')?.getAttribute('data-view') ?? null"), { timeoutMs: 10000, what: "the view in the list" });
            await ev("__wb.isolate(null)");
            await ev(`__wb.frame(${JSON.stringify(T.part)}, 30, 20, 0.3)`);
            await clickSelector(`[data-view="${id}"]`);
            await sleep(300);
            await frames(2);
            const now = await ev("__wb.cameraState()");
            const hiddenOthers = await ev(`Object.entries(__wb.viewer.getStates()).filter(([p]) => p.split('/')[2] !== ${JSON.stringify(T.part)}).every(([, s]) => s[0] === 0 && s[1] === 0)`);
            const view = (await wb("view-get", id)).json;
            return {
                ok: norm(sub(now.position, saved.position)) < 1e-6 && norm(sub(now.target, saved.target)) < 1e-6 && Math.abs(now.zoom - saved.zoom) < 1e-9 && hiddenOthers,
                view: id, hidden: view.hidden, positionErrorMm: norm(sub(now.position, saved.position)), hiddenOthersRestored: hiddenOthers,
            };
        }, { required: false });
        await ev("__wb.isolate(null)");
    }

    await step(`wbench show ${T.assembly} ${JSON.stringify(T.update)} is an incremental update`, async () => {
        const before = await ev("({ applied: __wb.appliedLog.length, received: __wb.received.length, scene: __wb.store().shown.scene })");
        const partLabels = await ev("Object.keys(__wb.viewer.getStates()).map((p) => p.split('/')[2]).filter((x, i, a) => a.indexOf(x) === i)");
        const boxesBefore = await ev(`Object.fromEntries(${JSON.stringify(partLabels)}.map((l) => [l, __wb.partBox(l)]))`);
        const t0 = Date.now();
        const shown = await wb("show", T.assembly, "--args", JSON.stringify(T.update));
        await waitUntil(() => ev(`__wb.appliedLog.length > ${before.applied}`), { timeoutMs: 60000, what: "the update in the page" });
        const totalMs = Date.now() - t0;
        const received = await ev(`__wb.received.slice(${before.received}).filter((r) => r.type === "wb_update")`);
        const applied = await ev("__wb.appliedLog[__wb.appliedLog.length - 1]");
        const boxesAfter = await ev(`Object.fromEntries(${JSON.stringify(partLabels)}.map((l) => [l, __wb.partBox(l)]))`);
        const shift = (l) => sub(boxesAfter[l].centre, boxesBefore[l].centre);
        const moved = shown.json.moved;
        const unmovedMax = Math.max(0, ...partLabels.filter((l) => !moved.includes(l) && !shown.json.changed.includes(l)).map((l) => norm(shift(l))));
        const movedShifts = moved.map((l) => shift(l));
        const uniform = movedShifts.length ? Math.max(...movedShifts.map((s) => norm(sub(s, movedShifts[0])))) : 0;
        return {
            ok: shown.json.incremental === true && received.length === 1 && applied.type === "wb_update" && unmovedMax < 1e-3,
            incremental: shown.json.incremental, moved, changed: shown.json.changed, messageBytes: received[0]?.bytes,
            hubMessageBytes: shown.json.message_bytes, partsBlobBytes: shown.json.blob?.bytes ?? 0,
            movedShift: movedShifts[0], movedShiftSpreadMm: uniform, unmovedMaxShiftMm: unmovedMax,
            cliMs: shown.ms, pageApplyMs: applied.ms, cliStartToPageAppliedMs: totalMs, kernelTimings: shown.json.timings,
        };
    }, { required: false });

    await step("kill the kernel: crash banner, scene stays", async () => {
        const state = await api("/api/state");
        const before = await ev("({ scene: __wb.store().shown.scene, leaves: __wb.leafCount(), groups: Object.keys(__wb.viewer.nestedGroup.groups).length })");
        const t0 = Date.now();
        process.kill(state.kernel.pid, "SIGKILL");
        await waitUntil(() => ev(`!!document.querySelector('[data-banner="crashed"]')`), { timeoutMs: 20000, what: "the crash banner" });
        const bannerMs = Date.now() - t0;
        await frames(2);
        const after = await ev("({ scene: __wb.store().shown.scene, leaves: __wb.leafCount(), groups: Object.keys(__wb.viewer.nestedGroup.groups).length, banner: document.querySelector('[data-banner=\"crashed\"]').textContent })");
        const hubState = await api("/api/state");
        writeFileSync(path.join(OUT, `e2e_${NAME}_crashed.png`), await page.screenshot());
        return {
            ok: after.scene === before.scene && after.leaves === before.leaves && after.groups === before.groups && hubState.kernel.state === "crashed" && hubState.scene.scene === before.scene,
            killedPid: state.kernel.pid, killToBannerMs: bannerMs, banner: after.banner.slice(0, 300), sceneKept: after.scene === before.scene,
        };
    }, { required: false });

    result.pass = result.steps.every((s) => s.ok);
} catch (e) {
    result.error = String(e.stack ?? e);
    console.error("ABORTED:", e.message);
} finally {
    result.pageConsole = page?.console?.slice(0, 30) ?? [];
    if (chrome) await stopChild(chrome.child);
    if (hub) {
        const t0 = Date.now();
        await stopChild(hub, { signal: "SIGTERM", graceMs: 15000 });
        result.hubStopMs = Date.now() - t0;
        result.hubExit = { code: hub.exitCode, signal: hub.signalCode };
    }
    await sleep(300);
    result.hubJsonLeft = existsSync(path.join(T.project, ".workbench", "hub.json"));
    result.kernelProcessesLeft = kernelProcesses();
    result.hubLog = hubLog.slice(-40);
    if (existsSync(path.join(T.project, "review"))) {
        rmSync(path.join(T.project, "review"), { recursive: true, force: true }); // created by this run (checked at the start)
        result.reviewRemoved = true;
    }
    await stopAll();
    result.finished = new Date().toISOString();
    save();
    console.log(`${result.pass ? "PASS" : "FAIL"}; results in ${path.join(OUT, `e2e_${NAME}.json`)}`);
}
