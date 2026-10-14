// Print the numbers RESULT.md quotes, straight from results/*.json (nothing is typed by hand).
//   node driver/summarize.mjs [results/s1_angle_vulkan_full.json ...]
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { RESULTS } from "./lib.mjs";

const files = process.argv.slice(2).length ? process.argv.slice(2) : readdirSync(RESULTS).filter((f) => /^s1_.*\.json$/.test(f)).map((f) => path.join(RESULTS, f));
const f1 = (x, d = 1) => (x === null || x === undefined || !Number.isFinite(x) ? "n/a" : x.toFixed(d));

for (const file of files) {
    const r = JSON.parse(readFileSync(file, "utf8"));
    console.log(`\n### ${path.basename(file)}  backend=${r.backend} suite=${r.suite}  ${r.started} .. ${r.finished}`);
    if (r.fatal) console.log("FATAL", r.fatal.slice(0, 300));
    const s = r.sections;
    if (s.first_show) {
        const fs = s.first_show;
        console.log(`first show: build ${f1(fs.host?.build_s, 2)} s, tessellate+convert ${f1(fs.host?.tessellate_convert_s, 2)} s, payload ${f1(fs.host?.payload_bytes / 1e6, 2)} MB, encode ${f1(fs.host?.encode_s * 1000, 1)} ms, ws send ${f1(fs.host?.ws_send_s * 1000, 1)} ms, backend encode+load ${f1((fs.host?.backend_encode_s + fs.host?.backend_load_s) * 1000, 0)} ms (${f1(fs.host?.backend_payload_bytes / 1e6, 2)} MB BREP); page parse ${f1(fs.page?.parseMs)} ms, build scene ${f1(fs.page?.handleMs)} ms, GPU done +${f1(fs.page?.gpuDoneMs)} ms; show->first frame ${f1(fs.showToFirstFrameMs, 0)} ms`);
    }
    if (s.q1_stock_ui) {
        const q = s.q1_stock_ui;
        console.log(`Q1 pass=${q.pass}: ` + ["tree", "toolbar", "treeToggle", "clipping", "measure", "explode", "getImage"].map((k) => `${k}=${q[k]?.pass}`).join(" "));
        if (q.gl) console.log(`   GL: ${q.gl.renderer}`);
        if (q.scene) console.log(`   scene: ${q.scene.leaves} leaves, ${q.scene.uniqueTriangles} unique triangles, ${q.scene.meshes} meshes; one frame draws ${q.frame?.triangles} triangles in ${q.frame?.calls} calls`);
        if (q.measure?.properties) console.log(`   measure: area panel shows exact ${q.measure.properties.exactArea?.toFixed(3)}: ${q.measure.properties.panelShowsExactArea}; distance exact ${q.measure.distance?.exactDistance?.toFixed(4)}: ${q.measure.distance?.panelShowsExactDistance}`);
        if (q.explode) console.log(`   explode moved ${q.explode.movedMm?.map((x) => x.toFixed(0)).join(",")} mm, restored err ${q.explode.restoredErrorMm?.map((x) => x.toExponential(0)).join(",")}`);
        if (q.clipping) console.log(`   clipping: pixels ${q.clipping.pixelsOpen} -> ${q.clipping.pixelsClipped}, slider ${q.clipping.sliderBefore} -> ${q.clipping.sliderAfter}, host saw ${q.clipping.hostSlider}`);
    }
    if (s.q2_picks) {
        const q = s.q2_picks;
        console.log(`Q2 pass=${q.pass}: ${q.nPass}/${q.nPicks} face picks over parts ${q.parts?.join(", ")}; max projection error ${f1(q.maxProjectionErrorPx, 3)} px (mean ${f1(q.meanProjectionErrorPx, 3)}), page vs python ${q.maxPageVsPythonPx?.toExponential(1)} px, point-to-face max ${q.maxPointToFaceMm?.toExponential(1)} mm, exact anchor vs aimed point max ${f1(q.maxAnchorToPointMm, 4)} mm; part picks (double-click) ${q.partPicks?.filter((x) => x.pass).length}/${q.partPicks?.length}`);
        if (q.error) console.log("   error:", q.error.slice(0, 300));
    }
    if (s.q3_overlay) {
        const q = s.q3_overlay;
        console.log(`Q3 pass=${q.pass}: anchor ${q.anchorFrom}; glue errors ${q.glue?.map((g) => `${f1(g.errorPx, 3)}px(${g.headPixels}/${f1(g.expectedDiscPx, 0)})`).join(", ")}`);
        if (q.occlusion) console.log(`   occluded view az ${q.occlusion.az} el ${q.occlusion.el} by ${q.occlusion.occluders?.join(", ")}: depth-tested pixels head/needle/arrow ${q.occlusion.withDepthTest?.head}/${q.occlusion.withDepthTest?.needle}/${q.occlusion.withDepthTest?.arrow}; control without depth test ${q.occlusion.controlNoDepthTest?.head}/${q.occlusion.controlNoDepthTest?.needle}/${q.occlusion.controlNoDepthTest?.arrow}, head at ${f1(q.occlusion.controlNoDepthTest?.headCentroidErrorPx, 3)} px`);
        if (q.overlayStats) console.log(`   overlay draws ${q.overlayStats.draws}, skipped render-target passes ${q.overlayStats.skippedTargets}`);
        if (q.error) console.log("   error:", q.error.slice(0, 300));
    }
    if (s.q5_incremental) {
        const q = s.q5_incremental;
        const a = q.liftTransform ?? {}, b = q.fullReshow ?? {}, c = q.liftReadd ?? {}, d = q.geometryUpdate ?? {};
        console.log(`Q5 pass=${q.pass}`);
        console.log(`   lift by transform: ${a.moved?.length} parts moved, ${a.regeom?.length} re-tessellated, ${a.bytesSent} B sent, host build ${f1(a.hostBuildS, 2)} s + diff ${f1(a.hostDiffS * 1000, 0)} ms + backend ${f1(a.hostBackendUpdateS * 1000, 0)} ms, page apply ${f1(a.pageApplyMs)} ms, send->GPU done ${f1(a.latencyFromSendMs)} ms, box vs exact ${a.maxBoxDiffVsExactMm?.toExponential(1)} mm, pick after: ${a.pickAfterMove?.pass} (${a.pickAfterMove?.id})`);
        console.log(`   full re-show:      ${f1(b.payloadBytes / 1e6, 2)} MB, tessellate ${f1(b.tessellateConvertS, 2)} s (warm cache), page build ${f1(b.pageHandleMs)} ms, show->frame ${f1(b.showToFirstFrameMs, 0)} ms; incremental vs full ${b.maxBoxDiffIncrementalVsFullMm?.toExponential(1)} mm`);
        console.log(`   lift by re-add:    ${c.moved?.length} parts, ${c.bytesSent} B, page apply ${f1(c.pageApplyMs)} ms, leaves ${c.leavesAfter}, box vs exact ${c.maxBoxDiffVsExactMm?.toExponential(1)} mm, pick after: ${c.pickAfterReadd?.pass}`);
        console.log(`   geometry change ${JSON.stringify(d.change)}: re-tessellated ${d.regeom?.join(",")}, moved ${d.moved?.length}, ${d.bytesSent} B, tessellate ${f1(d.hostTessellateS * 1000, 0)} ms, page decode+updatePart ${f1(d.pageDecodeApplyMs)} ms, leaves ${d.leavesAfter}, box vs exact ${d.maxBoxDiffVsExactMm?.toExponential(1)} mm, pick after: ${d.pickAfterGeom?.pass}`);
    }
    for (const [k, v] of Object.entries(s).filter(([k]) => k.startsWith("q4_perf"))) {
        if (!v.ok) {
            console.log(`${k}: FAILED ${v.error?.slice(0, 200)}`);
            continue;
        }
        const o = v.orbitRaf, sy = v.orbitSync, gt = v.orbitGpuTimer;
        console.log(`${k}: canvas ${v.canvas?.join("x")} | ${v.gl?.renderer}`);
        console.log(`   show->first frame cold ${f1(v.show?.cold?.showToFirstFrameMs, 0)} ms (tess ${f1(v.show?.cold?.host?.tessellate_convert_s, 2)} s, page build ${f1(v.show?.cold?.page?.handleMs, 0)} ms, GPU ${f1(v.show?.cold?.page?.gpuDoneMs, 0)} ms), warm ${f1(v.show?.warm?.showToFirstFrameMs, 0)} ms`);
        console.log(`   orbit rAF: median ${f1(o?.interval?.median, 2)} ms, p95 ${f1(o?.interval?.p95, 2)}, max ${f1(o?.interval?.max, 1)}, mean ${f1(o?.fpsMean, 1)} fps, >25ms: ${o?.over16_7ms}/${o?.interval?.n}; update() CPU median ${f1(o?.updateCpu?.median, 2)} ms`);
        console.log(`   sync (render+readPixels): median ${f1(sy?.updateCpu?.median, 2)} ms, p95 ${f1(sy?.updateCpu?.p95, 2)}; GPU timer median ${f1(gt?.gpuTimer?.median, 2)} ms p95 ${f1(gt?.gpuTimer?.p95, 2)}; one frame draws ${v.frame?.triangles} triangles in ${v.frame?.calls} calls`);
    }
}
