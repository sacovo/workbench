"""Q1: the robot's compiled model as viewer parts. Times each stage, sizes the wire message, and
checks every leaf's world placement against MuJoCo's own geom_xpos / geom_xmat.

Run from the robot snapshot:  env -u VIRTUAL_ENV uv run python <spike>/py/q1_convert.py
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mujoco  # noqa: E402
import numpy as np  # noqa: E402
import orjson  # noqa: E402

t0 = time.perf_counter()
import robot  # noqa: E402
from robot import mjcf  # noqa: E402

t_import = time.perf_counter() - t0

import sim_scene  # noqa: E402

RESULTS = HERE.parent / "results"


def leaf_world_check(model, data, scene):
    """Leaf world pose from the scene tree (body loc then leaf loc) against geom_xpos / geom_xmat."""
    shapes = scene.message["data"]["shapes"]
    worst_p, worst_r, n = 0.0, 0.0, 0
    for node in shapes["parts"]:
        bp, bq = np.array(node["loc"][0]), node["loc"][1]
        Rb = np.zeros(9)
        mujoco.mju_quat2Mat(Rb, np.array([bq[3], bq[0], bq[1], bq[2]]))
        Rb = Rb.reshape(3, 3)
        for leaf in node["parts"]:
            g = scene.leaves[leaf["id"]]["geom"]
            lp, lq = np.array(leaf["loc"][0]), leaf["loc"][1]
            Rl = np.zeros(9)
            mujoco.mju_quat2Mat(Rl, np.array([lq[3], lq[0], lq[1], lq[2]]))
            p = Rb @ lp + bp
            R = Rb @ Rl.reshape(3, 3)
            worst_p = max(worst_p, float(np.abs(p / 1000 - data.geom_xpos[g]).max()))
            worst_r = max(worst_r, float(np.abs(R - data.geom_xmat[g].reshape(3, 3)).max()))
            n += 1
    return {"leaves_checked": n, "max_position_error_m": worst_p, "max_rotation_matrix_error": worst_r}


def main():
    t0 = time.perf_counter()
    spec = mjcf.robot_spec(payload=0.15)
    t_spec = time.perf_counter() - t0
    t0 = time.perf_counter()
    model = spec.compile()
    t_compile = time.perf_counter() - t0
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)

    out = {
        "robot_file": robot.__file__,
        "model": {"nbody": model.nbody, "ngeom": model.ngeom, "nmesh": model.nmesh, "nmeshvert": model.nmeshvert,
                  "nmeshface": model.nmeshface, "timestep": model.opt.timestep},
        "robot_import_s": t_import, "robot_spec_s": t_spec, "compile_s": t_compile,
        "variants": [],
    }
    # a small config of the size the viewer's pipeline sends, only for sizing the message
    config = {"_splash": False, "collapse": 1, "reset_camera": "reset", "render_edges": True, "normal_len": 0}
    for which in ("visual", "collision"):
        for primitives in ("direct", "brep"):
            for rep in range(2):  # second run: ocp-tessellate's cache and warm imports
                t0 = time.perf_counter()
                scene = sim_scene.build_scene(model, data, which=which, primitives=primitives)
                t_build = time.perf_counter() - t0
                t0 = time.perf_counter()
                wire = orjson.dumps(sim_scene.message_for(scene, config), default=str)
                t_dump = time.perf_counter() - t0
                row = {
                    "which": which, "primitives": primitives, "repeat": rep,
                    "build_s": t_build, "orjson_dumps_s": t_dump, "wire_bytes": len(wire),
                    "stats": scene.stats, "world_check": leaf_world_check(model, data, scene),
                }
                out["variants"].append(row)
                print(f"{which:9s} {primitives:6s} rep{rep}: build {t_build:.3f}s dumps {t_dump:.3f}s "
                      f"wire {len(wire)/1e6:.2f} MB leaves {scene.stats['leaves']} instances {scene.stats['instances']} "
                      f"drawn tris {scene.stats['drawn_triangles']} world err {row['world_check']['max_position_error_m']:.2e} m",
                      flush=True)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "q1_conversion.json").write_text(json.dumps(out, indent=1, default=float))
    print("wrote", RESULTS / "q1_conversion.json")


if __name__ == "__main__":
    main()
