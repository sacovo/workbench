"""Collect the key numbers of every results/*.json into results/summary.json (and print them).

python3 py/summarize.py   (plain Python, no project imports)
"""

import json
from pathlib import Path

R = Path(__file__).resolve().parent.parent / "results"


def load(name):
    f = R / name
    return json.loads(f.read_text()) if f.exists() else None


def main():
    out = {}
    q1 = load("q1_conversion.json")
    out["q1"] = {
        "model": q1["model"], "robot_import_s": q1["robot_import_s"], "robot_spec_s": q1["robot_spec_s"], "compile_s": q1["compile_s"],
        "variants": [{k: v[k] for k in ("which", "primitives", "repeat", "build_s", "wire_bytes")}
                     | {"leaves": v["stats"]["leaves"], "instances": v["stats"]["instances"], "unique_vertices": v["stats"]["unique_vertices"],
                        "unique_triangles": v["stats"]["unique_triangles"], "drawn_triangles": v["stats"]["drawn_triangles"],
                        "tessellation_s_by_kind": v["stats"]["tessellation_s_by_kind"], "world_err_m": v["world_check"]["max_position_error_m"]}
                     for v in q1["variants"]],
    }
    q2 = load("q2_recording.json")
    out["q2"] = {"trip_limit": q2["record"]["trip_limit"], "reference_poses_s": q2["poses"].get("reference_poses_s")}
    for k in ("swing", "trip_above", "trip_below"):
        m = q2["record"][k]
        out["q2"][k] = {kk: m.get(kk) for kk in ("recipe", "samples", "t_last", "end_time", "steps", "raw_total_bytes", "raw_bytes", "npz_bytes",
                                                   "peak_tilt_from_recording_deg", "final_tilt_from_recording_deg", "fk_consistency",
                                                   "bit_identical_to_unrecorded", "returned_peak_tilt_deg", "returned_falls", "unrecorded_falls",
                                                   "sample_intervals_s", "wall_s_recorded", "wall_s_unrecorded")}
    runs = {}
    for f in sorted(R.glob("s5_*.json")):
        d = json.loads(f.read_text())
        row = {"gl": d.get("gl"), "window": d.get("window"), "renderer": (d.get("glInfo") or {}).get("renderer"), "canvas": (d.get("glInfo") or {}).get("canvas"),
               "viewerAccess": d.get("viewerAccess"), "error": d.get("error")}
        if d.get("load"):
            row["load"] = [{"which": r["which"], "primitives": r["primitives"], "total_ms": round(r["total_ms"]), "from_send_ms": round(r["from_send_ms"]),
                            "python_build_ms": round(r["python"]["build_s"] * 1000), "python_send_ms": round(r["python"]["send_ms"]),
                            "page_recv_ms": round(r["page"]["recv_after_send_start_ms"]), "page_handle_ms": round(r["page"]["handle_ms"]),
                            "gpu_ms": round(r["page"]["gpu_ms"]), "wire_MB": round(r["python"]["wire_bytes"] / 1e6, 2),
                            "triangles": r["counts"]["frontTriangles"]} for r in d["load"]["rows"]]
            row["load_pass_under_5s"] = d["load"]["pass_under_5s"]
        if d.get("play"):
            row["play"] = {k: {"frames": v["frames"], "fps": round(v["fps"], 1), "median_ms": v["interval"]["median"], "p95_ms": v["interval"]["p95"],
                               "max_ms": v["interval"]["max"], "over25ms": v["over25ms"], "over50ms": v["over50ms"], "clock_rate": round(v["clockRate"], 4),
                               "smooth": v["smooth"], "anim_wire_bytes": v["anim"]["wire_bytes"], "tracks": v["anim"]["tracks"], "keyframes": v["anim"]["keyframes"]}
                           for k, v in d["play"].items() if k != "criteria"}
        if d.get("repeat"):
            rp = d["repeat"]
            row["repeat"] = {k: rp[k] for k in ("totalFrames", "totalOver20ms", "totalOver50ms", "worstMs", "smoothRuns")}
        if d.get("poses"):
            row["poses"] = {k: {"via": v["via"], "samples": v["samples"], "body_poses": v["body_poses_compared"], "max_pos_err_m": v["max_pos_err_m"],
                                "max_angle_err_rad": v["max_angle_err_rad"], "leaf_poses": v["leaves"]["leaf_poses_compared"],
                                "max_leaf_pos_err_m": v["leaves"]["max_leaf_pos_err_m"], "pass": v["pass_1e-6_m"]}
                            for k, v in d["poses"].items() if isinstance(v, dict) and "via" in v}
        if d.get("pick"):
            pk = d["pick"]
            row["pick"] = {"candidates": pk["candidatesVisibleAtCentre"], "picks": len(pk["picks"]), "match_raycast": pk["nPass"],
                           "map_back_to_geom_and_body": pk.get("nMapBack"), "python_sees_last_pick": pk["pythonSeesLastPick"],
                           "misses": [{"expected": m["expected"], "got": m["got"], "sizePx": m.get("sizePx"), "raycastSecond": m.get("raycastSecond"),
                                       "raycastSecondGapMm": m.get("raycastSecondGapMm")} for m in pk["picks"] if not m["pass"]]}
        if d.get("slider"):
            row["slider"] = {k: {"n": len(v["rows"]), "shown_median_ms": round(v["shown"]["median"], 1), "shown_p95_ms": round(v["shown"]["p95"], 1),
                                 "shown_max_ms": round(v["shown"]["max"], 1),
                                 "mj_forward_ms_median": round(sorted(r["mj_forward_ms"] for r in v["rows"])[len(v["rows"]) // 2], 3),
                                 "send_ms_median": round(sorted(r["send_ms"] for r in v["rows"])[len(v["rows"]) // 2], 2),
                                 "recv_ms_median": round(sorted(r["recv_ms"] for r in v["rows"])[len(v["rows"]) // 2], 2),
                                 "pos_err_by_update_m": v["posErrByUpdate"]} for k, v in d["slider"].items()}
        if d.get("clear"):
            row["clear"] = d["clear"]
        runs[f.stem] = row
    out["runs"] = runs
    (R / "summary.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
