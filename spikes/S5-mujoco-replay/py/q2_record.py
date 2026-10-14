"""Q2: record the swing and the trip pair with the mj_step recorder (no viewer needed).

Writes results/recording_{swing,trip_above,trip_below}.npz, results/poses_payload0.15.json (cache
of reference_poses) and results/q2_recording.json.

Run from the robot snapshot:  env -u VIRTUAL_ENV uv run python <spike>/py/q2_record.py
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import s5host  # noqa: E402  (its module setup sends prints to stderr)


def main():
    host = s5host.Host(port=0)  # the port is only used for viewer messages, which this script never sends
    t0 = time.perf_counter()
    res = {"init": host.op_init(payload=0.15)}
    poses = host.op_poses()
    res["poses"] = {k: v for k, v in poses.items() if k != "poses"} | {"values": poses["poses"]}
    res["record"] = host.op_record()
    res["wall_s"] = time.perf_counter() - t0
    out = HERE.parent / "results" / "q2_recording.json"
    out.write_text(json.dumps(res, indent=1, default=float))
    print(f"wrote {out} in {res['wall_s']:.1f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
