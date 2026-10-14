"""Tiny study: tilt of the fixture after 1 s of settling, at three lever start angles.

Usage: uv run scripts/study.py
"""

import json
import math

import mujoco

from minimal import mjcf
from minimal.paths import build_dir

ANGLES_DEG = [-45.0, 0.0, 45.0]


def tilt_deg(data) -> float:
    """Angle between the fixture's vertical and the world vertical."""
    _, x, y, _ = data.qpos[3:7]
    return math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * (x * x + y * y)))))


def settle(model, data, lever_angle_rad: float, seconds: float = 1.0) -> float:
    mujoco.mj_resetData(model, data)
    data.qpos[model.joint("lever_hinge").qposadr[0]] = lever_angle_rad
    data.ctrl[model.actuator("lever_hinge").id] = lever_angle_rad
    mujoco.mj_forward(model, data)
    while data.time < seconds:
        mujoco.mj_step(model, data)
    return tilt_deg(data)


if __name__ == "__main__":
    model = mjcf.minimal_spec().compile()
    data = mujoco.MjData(model)
    results = [
        {"lever_angle_deg": angle, "tilt_after_1s_deg": round(settle(model, data, math.radians(angle)), 4)}
        for angle in ANGLES_DEG
    ]

    out = build_dir()
    (out / "study.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
