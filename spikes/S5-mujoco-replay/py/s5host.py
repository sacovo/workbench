"""Spike S5 kernel-side host: owns the compiled model, the recordings and the sim scene, and pushes
messages to a running stock `ocp_viewer` server through ocp_viewer's own client (`comms`).

Control protocol: one JSON object per line on stdin ({"id", "op", "args"}), one reply per line on
the original stdout ({"id", "ok", "result" | "error"}). Everything else printed (ocp_viewer's
own messages) goes to stderr.

Run from the robot snapshot:  env -u VIRTUAL_ENV uv run python <spike>/py/s5host.py --port 3959
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

HERE = Path(__file__).resolve().parent
SPIKE = HERE.parent
RESULTS = SPIKE / "results"
sys.path.insert(0, str(HERE))

import mujoco  # noqa: E402
import numpy as np  # noqa: E402
import orjson  # noqa: E402

import recorder  # noqa: E402
import sim_scene  # noqa: E402
import tracks as T  # noqa: E402


def wall_ms():
    return time.time() * 1000.0


class Host:
    def __init__(self, port):
        self.port = port
        self.model = None
        self.data = None
        self.scene = None
        self.runs = {}  # name -> dict(arrays, meta)
        self.held = None  # a websocket held open to the server (Q6 "direct_held")
        self.anim = None  # (run name, track list) currently loaded in the page

    # ----------------------------------------------------------------------------------------
    def op_init(self, payload=0.15):
        t0 = time.perf_counter()
        import robot
        from robot import mjcf

        t_import = time.perf_counter() - t0
        import ocp_viewer
        from ocp_viewer.comms import comms

        ocp_viewer.set_port(self.port)
        self.comms = comms
        self.show_module = sys.modules["ocp_viewer.show"]
        t0 = time.perf_counter()
        spec = mjcf.robot_spec(payload=payload)
        self.model = spec.compile()
        t_build = time.perf_counter() - t0
        self.data = mujoco.MjData(self.model)
        mujoco.mj_forward(self.model, self.data)
        self.payload = payload
        return {"robot_file": robot.__file__, "import_s": t_import, "spec_and_compile_s": t_build,
                "nbody": self.model.nbody, "ngeom": self.model.ngeom, "port": self.port}

    def op_poses(self):
        from robot import stability

        cache = RESULTS / f"poses_payload{self.payload}.json"
        if cache.exists():
            poses = json.loads(cache.read_text())
            return {"cached": True, "names": list(poses), "poses": poses}
        t0 = time.perf_counter()
        poses = stability.reference_poses(self.model)
        elapsed = time.perf_counter() - t0
        plain = {k: ({s: list(map(float, v[s])) for s in v} if isinstance(v, dict) else list(map(float, v))) for k, v in poses.items()}
        RESULTS.mkdir(exist_ok=True)
        cache.write_text(json.dumps(plain, indent=1))
        return {"cached": False, "reference_poses_s": elapsed, "names": list(plain), "poses": plain}

    # ----------------------------------------------------------------------------------------
    def _store(self, name, rec_run, meta):
        arr = rec_run.arrays()
        self.runs[name] = {"arrays": arr, "meta": meta}
        np.savez_compressed(RESULTS / f"recording_{name}.npz", **arr)
        raw = {k: int(v.nbytes) for k, v in arr.items()}
        npz = (RESULTS / f"recording_{name}.npz").stat().st_size
        chassis = self.model.body("chassis").id
        q = arr["xquat"][:, chassis]  # w x y z
        tilt = np.degrees(np.arccos(np.clip(1 - 2 * (q[:, 1] ** 2 + q[:, 2] ** 2), -1, 1)))
        # every sample's poses are the kinematics of its own qpos (time stamps are consistent)
        scratch = mujoco.MjData(self.model)
        fk_p, fk_q = 0.0, 0.0
        for k in range(len(arr["times"])):
            scratch.qpos[:] = arr["qpos"][k]
            mujoco.mj_kinematics(self.model, scratch)
            fk_p = max(fk_p, float(np.abs(scratch.xpos - arr["xpos"][k]).max()))
            fk_q = max(fk_q, float(np.abs(scratch.xquat - arr["xquat"][k]).max()))
        # what pairing data.time / data.qpos after mj_step with data.xpos would be off by: one step
        speed = np.linalg.norm(np.diff(arr["xpos"], axis=0), axis=2).max(axis=1) / np.diff(arr["times"])[:, None].ravel()
        meta["fk_consistency"] = {"max_xpos_err_m": fk_p, "max_xquat_err": fk_q,
                                  "peak_body_speed_m_s": float(speed.max()),
                                  "naive_one_step_stamp_error_m": float(speed.max() * self.model.opt.timestep)}
        meta.update({"samples": len(arr["times"]), "t_first": float(arr["times"][0]), "t_last": float(arr["times"][-1]),
                     "end_time": rec_run.end_time, "steps": rec_run.steps, "raw_bytes": raw,
                     "raw_total_bytes": int(sum(raw.values())), "npz_bytes": int(npz),
                     "peak_tilt_from_recording_deg": float(tilt.max()), "final_tilt_from_recording_deg": float(tilt[-1]),
                     "mean_sample_interval_s": float(np.diff(arr["times"]).mean()),
                     "sample_intervals_s": sorted({round(float(d), 6) for d in np.diff(arr["times"])})})
        return meta

    def op_record(self, trip_pose="stretched", trip_direction=0.0, swing=("up", "stretched"), rate_hz=60.0):
        """The swing at the arm speed limit, and a trip just above (and just below) its limit."""
        from robot import stability
        from robot.params import LIFT, RULES

        poses = self.op_poses()["poses"]
        top = LIFT.travel * 1e-3
        m = self.model
        out = {"top_lift_m": top}

        # determinism: the same swing without and with the recorder
        t0 = time.perf_counter()
        plain_tilt = stability.arm_swing(m, poses[swing[0]], poses[swing[1]], top, RULES.arm_speed_limit)
        t_plain = time.perf_counter() - t0
        t0 = time.perf_counter()
        with recorder.recording(m, rate_hz) as rec:
            rec_tilt = stability.arm_swing(m, poses[swing[0]], poses[swing[1]], top, RULES.arm_speed_limit)
        t_rec = time.perf_counter() - t0
        assert len(rec.runs) == 1, [len(r.times) for r in rec.runs]
        out["swing"] = self._store("swing", rec.runs[0], {
            "recipe": f"stability.arm_swing(model, poses[{swing[0]!r}], poses[{swing[1]!r}], lift={top}, joint_speed=RULES.arm_speed_limit={RULES.arm_speed_limit})",
            "returned_peak_tilt_deg": rec_tilt, "unrecorded_peak_tilt_deg": plain_tilt,
            "bit_identical_to_unrecorded": bool(rec_tilt == plain_tilt), "wall_s_unrecorded": t_plain, "wall_s_recorded": t_rec,
            "foreign_steps": rec.foreign_steps})

        # the trip limit by the study's own bisection, then one run above and one below it
        falls = lambda v: stability.falls_when_tripping(m, poses[trip_pose], top, v, trip_direction)  # noqa: E731
        t0 = time.perf_counter()
        lim = stability.limit(falls, 0.0, 2.0)
        t_lim = time.perf_counter() - t0
        step = 2.0 / 2 ** 8  # the bisection's final bracket width
        out["trip_limit"] = {"pose": trip_pose, "direction_deg": trip_direction, "limit_m_s": lim, "bracket_m_s": step, "bisection_wall_s": t_lim}
        for name, speed in (("trip_above", lim + step), ("trip_below", lim)):
            plain = falls(speed)
            t0 = time.perf_counter()
            with recorder.recording(m, rate_hz) as rec:
                fell = falls(speed)
            t_rec = time.perf_counter() - t0
            assert len(rec.runs) == 1
            out[name] = self._store(name, rec.runs[0], {
                "recipe": f"stability.falls_when_tripping(model, poses[{trip_pose!r}], lift={top}, speed={speed}, direction_deg={trip_direction})",
                "speed_m_s": speed, "returned_falls": fell, "unrecorded_falls": plain, "wall_s_recorded": t_rec})
        return out

    def op_load_runs(self):
        out = {}
        for name in ("swing", "trip_above", "trip_below"):
            f = RESULTS / f"recording_{name}.npz"
            if f.exists():
                z = np.load(f)
                self.runs[name] = {"arrays": {k: z[k] for k in z.files}, "meta": {}}
                out[name] = len(z["times"])
        return out

    # ----------------------------------------------------------------------------------------
    def _config(self):
        """The config the viewer's own show pipeline would send (taken from a one-box show)."""
        from build123d import Box

        msg, _ = self.show_module.viewer._convert(Box(1, 1, 1))
        return msg["config"]

    def op_config(self):
        return self._config()

    def _send(self, msg, held=False):
        """Through the session, as `show` and `Animation.animate` send: it renames the config
        block to the page's camelCase names and adds the version handshake. Sending a config in
        snake_case is silently ignored by the page (`reset_camera` became "keep")."""
        if held:
            assert "config" not in msg, "the held path is only for config-free messages"
            if self.held is None:
                from websockets.sync.client import connect

                self.held = connect(f"ws://127.0.0.1:{self.port}", close_timeout=0.05, max_size=None)
            self.held.send(b"D:" + orjson.dumps(msg))
        else:
            self.show_module.viewer.config.session.send_data(msg)

    def op_show(self, which="visual", primitives="direct", reset_camera="reset", base=None, tag=None):
        t_start = wall_ms()
        t0 = time.perf_counter()
        data = self.data
        if base:  # "run_name:k" -> scene at that recorded sample
            name, k = base.split(":")
            arr = self.runs[name]["arrays"]
            data = mujoco.MjData(self.model)
            data.qpos[:] = arr["qpos"][int(k)]
            mujoco.mj_kinematics(self.model, data)
        scene = sim_scene.build_scene(self.model, data, which=which, primitives=primitives)
        t_build = time.perf_counter() - t0
        t0 = time.perf_counter()
        config = self._config()
        t_config = time.perf_counter() - t0
        msg = sim_scene.message_for(scene, config, reset_camera)
        msg["s5"] = {"tag": tag or f"show-{which}-{primitives}", "t0": t_start}
        t0 = time.perf_counter()
        wire = orjson.dumps(msg)
        t_dumps = time.perf_counter() - t0
        t_send0 = wall_ms()
        self._send(msg)
        t_send1 = wall_ms()
        self.scene = scene
        self.scene_config = config
        self.anim = None
        return {"t_start_wall_ms": t_start, "t_send_start_wall_ms": t_send0, "t_send_end_wall_ms": t_send1,
                "build_s": t_build, "config_s": t_config, "dumps_s": t_dumps, "wire_bytes": len(wire),
                "stats": scene.stats, "body_paths": {str(b): p for b, p in scene.body_paths.items()},
                "leaf_count": len(scene.leaves), "root": scene.root}

    def op_animate(self, run="swing", speed=1.0, tag=None):
        arr = self.runs[run]["arrays"]
        t0 = time.perf_counter()
        trs = T.body_tracks(self.scene, arr["times"], arr["xpos"], arr["xquat"])
        msg = T.animation_message(trs, speed)
        t_tracks = time.perf_counter() - t0
        msg["s5"] = {"tag": tag or f"animate-{run}", "t0": wall_ms()}
        wire = orjson.dumps(msg)
        t_send0 = wall_ms()
        self._send(msg)
        t_send1 = wall_ms()
        self.anim = (run, trs)
        return {"tracks": len(trs), "bodies": len(trs) // 2, "keyframes": len(arr["times"]), "duration_s": float(arr["times"][-1] - arr["times"][0]),
                "build_tracks_s": t_tracks, "wire_bytes": len(wire), "t_send_start_wall_ms": t_send0, "t_send_end_wall_ms": t_send1,
                "track_paths": sorted({tr[0] for tr in trs})}

    def op_animate_helper(self, run="swing", speed=1.0):
        """The same tracks through ocp_viewer's own `Animation` helper (ocp_viewer_core/animation.py).

        The helper accepts only paths of the last `show` (`viewer.get_last_paths()`); this scene is
        not sent by `show`, so its paths are put there first. The helper then validates every track
        and sends the same 'animation' message (without our tag)."""
        arr = self.runs[run]["arrays"]
        sm = self.show_module
        sm.viewer.last_paths = [self.scene.root, *self.scene.body_paths.values(), *self.scene.leaves]
        trs = T.body_tracks(self.scene, arr["times"], arr["xpos"], arr["xquat"])
        t0 = time.perf_counter()
        anim = sm.Animation()
        for path, action, times, values in trs:
            anim.add_track(path, action, times, values)
        t_add = time.perf_counter() - t0
        t_send0 = wall_ms()
        anim.animate(speed)
        t_send1 = wall_ms()
        return {"tracks": len(anim.tracks), "max_duration": anim.max_duration, "add_track_s": t_add,
                "t_send_start_wall_ms": t_send0, "t_send_end_wall_ms": t_send1}

    def op_compare_leaves(self, run, readings):
        """Leaf (geom) world poses in the page against MuJoCo's geom_xpos / geom_xmat, from forward
        kinematics of the recorded qpos: what is drawn against where MuJoCo has each geom."""
        arr = self.runs[run]["arrays"]
        scratch = mujoco.MjData(self.model)
        worst_p, worst_r, n = 0.0, 0.0, 0
        for r in readings:
            scratch.qpos[:] = arr["qpos"][r["k"]]
            mujoco.mj_kinematics(self.model, scratch)
            for path, pose in r["leaves"].items():
                g = self.scene.leaves[path]["geom"]
                worst_p = max(worst_p, float(np.abs(np.array(pose["p"]) / 1000 - scratch.geom_xpos[g]).max()))
                q = pose["q"]
                R = np.zeros(9)
                mujoco.mju_quat2Mat(R, np.array([q[3], q[0], q[1], q[2]]) / np.linalg.norm(q))
                worst_r = max(worst_r, float(np.abs(R - scratch.geom_xmat[g]).max()))
                n += 1
        return {"leaf_poses_compared": n, "max_leaf_pos_err_m": worst_p, "max_leaf_rotmat_err": worst_r}

    def op_set_relative_time(self, fraction, tag=None):
        t0 = wall_ms()
        self.comms.send_command({"type": "set_relative_time", "value": float(fraction), "s5": {"tag": tag or "srt", "t0": t0}})
        return {"t_send_start_wall_ms": t0, "t_send_end_wall_ms": wall_ms()}

    def op_compare(self, run, readings):
        """Page readings ({k, poses: {path: {p, q}}}) against the recorded xpos / xquat."""
        arr = self.runs[run]["arrays"]
        by_path = {p: b for b, p in self.scene.body_paths.items()}
        worst_p, worst_ang, rows, n = 0.0, 0.0, [], 0
        worst_where = None
        for r in readings:
            k = r["k"]
            row_p, row_a = 0.0, 0.0
            for path, pose in r["poses"].items():
                b = by_path[path]
                p_page = np.array(pose["p"]) / 1000.0
                err = float(np.abs(p_page - arr["xpos"][k, b]).max())
                qp = np.array(pose["q"])  # x y z w
                qr = arr["xquat"][k, b][[1, 2, 3, 0]]
                ang = float(2 * np.arccos(np.clip(abs(np.dot(qp / np.linalg.norm(qp), qr)), -1, 1)))
                n += 1
                if err > worst_p:
                    worst_p, worst_where = err, {"k": k, "path": path, "t": float(arr["times"][k])}
                worst_ang = max(worst_ang, ang)
                row_p, row_a = max(row_p, err), max(row_a, ang)
            rows.append({"k": k, "t_rec": float(arr["times"][k] - arr["times"][0]), "t_page": r.get("tPage"),
                         "max_pos_err_m": row_p, "max_angle_err_rad": row_a})
        return {"run": run, "samples": len(readings), "body_poses_compared": n, "max_pos_err_m": worst_p,
                "max_angle_err_rad": worst_ang, "worst": worst_where, "rows": rows, "pass_1e-6_m": worst_p <= 1e-6}

    def op_expected_poses(self, run, k):
        arr = self.runs[run]["arrays"]
        return {p: {"p": (arr["xpos"][k, b] * 1000).tolist(), "q": arr["xquat"][k, b][[1, 2, 3, 0]].tolist()} for b, p in self.scene.body_paths.items()}

    # ----------------------------------------------------------------------------------------
    def op_slider(self, mechanism, joint="left_shoulder_lift", value=0.0, seq=0, series=""):
        """Change one joint, mj_forward, push the new body transforms by `mechanism`.

        reshow       full data message with every body group's loc changed (stock page, stock server)
        animation    a one-pose animation message + set_relative_time (stock page, stock server)
        direct       our 's5_body_poses' message through the stock server's data channel, a new
                     connection per message like ocp_viewer's comms (needs page code: harness.js)
        direct_held  the same over one websocket held open to the stock server
        """
        t0 = wall_ms()
        tp = time.perf_counter()
        m, d = self.model, self.data
        adr = m.joint(joint).qposadr[0]
        d.qpos[adr] = value
        mujoco.mj_forward(m, d)
        t_fwd = (time.perf_counter() - tp) * 1000
        tag = {"tag": f"slider-{series}-{mechanism}-{seq}", "t0": t0, "seq": seq}
        expected = {p: {"p": (d.xpos[b] * 1000).tolist(), "q": d.xquat[b][[1, 2, 3, 0]].tolist()} for b, p in self.scene.body_paths.items()}
        tb = time.perf_counter()
        if mechanism == "reshow":
            msg = sim_scene.with_body_locs(self.scene, d.xpos, d.xquat)
            msg = {**msg, "config": {**self.scene_config, "reset_camera": "keep"}, "s5": tag}
            t_build = (time.perf_counter() - tb) * 1000
            t_s0 = wall_ms()
            self._send(msg)
        elif mechanism == "animation":
            trs = T.body_tracks(self.scene, [0.0, 1.0], np.stack([d.xpos, d.xpos]), np.stack([d.xquat, d.xquat]), moving_only=False)
            msg = {**T.animation_message(trs, 1.0), "s5": tag}
            t_build = (time.perf_counter() - tb) * 1000
            t_s0 = wall_ms()
            self._send(msg)
            self.comms.send_command({"type": "set_relative_time", "value": 0.5, "s5": {**tag, "part": "srt"}})
        elif mechanism in ("direct", "direct_held"):
            poses = {p: [(d.xpos[b] * 1000).tolist(), d.xquat[b][[1, 2, 3, 0]].tolist()] for b, p in self.scene.body_paths.items()}
            msg = {"type": "s5_body_poses", "poses": poses, "s5": tag}
            t_build = (time.perf_counter() - tb) * 1000
            t_s0 = wall_ms()
            self._send(msg, held=(mechanism == "direct_held"))
        else:
            raise ValueError(mechanism)
        t_s1 = wall_ms()
        return {"t0_wall_ms": t0, "mj_forward_ms": t_fwd, "build_ms": t_build, "t_send_start_wall_ms": t_s0,
                "t_send_end_wall_ms": t_s1, "expected": expected, "tag": tag["tag"]}

    def op_compare_poses(self, expected, poses):
        worst_p, worst_a = 0.0, 0.0
        for path, e in expected.items():
            got = poses.get(path)
            if got is None:
                return {"missing": path}
            worst_p = max(worst_p, float(np.abs(np.array(got["p"]) - np.array(e["p"])).max()) / 1000)
            qp, qe = np.array(got["q"]), np.array(e["q"])
            worst_a = max(worst_a, float(2 * np.arccos(np.clip(abs(np.dot(qp / np.linalg.norm(qp), qe)), -1, 1))))
        return {"max_pos_err_m": worst_p, "max_angle_err_rad": worst_a}

    def op_status(self):
        st = self.comms.status()
        return {"lastPick": (st or {}).get("lastPick"), "keys": sorted((st or {}).keys())}

    def op_map_pick(self, path, name):
        hit = self.scene.pick_to_geom(path, name)
        if hit is None:
            return None
        g = hit["geom"]
        m = self.model
        return {**hit, "consistent": bool(m.geom_bodyid[g] == hit["body"] and m.body(hit["body"]).name == hit["body_name"]
                                          and m.geom(g).name == hit["geom_name"])}

    def op_leaf_of_geom(self, geom):
        for path, info in self.scene.leaves.items():
            if info["geom"] == geom:
                return path
        return None

    def op_close(self):
        if self.held is not None:
            self.held.close()
            self.held = None
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    host = Host(args.port)
    _proto.write(json.dumps({"ready": True}) + "\n")
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            fn = getattr(host, "op_" + req["op"])
            res = fn(**req.get("args", {}))
            _proto.write(orjson.dumps({"id": req["id"], "ok": True, "result": res}, option=orjson.OPT_SERIALIZE_NUMPY).decode() + "\n")
        except Exception as e:  # noqa: BLE001
            _proto.write(json.dumps({"id": req["id"], "ok": False, "error": repr(e), "trace": traceback.format_exc()}) + "\n")
        if req["op"] == "close":
            break


if __name__ == "__main__":
    main()
