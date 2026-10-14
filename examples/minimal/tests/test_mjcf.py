"""The MJCF scene compiles with the expected bodies, and the free body settles instead of falling."""

import math

import mujoco

from minimal import mjcf


def test_mjcf_compiles_with_the_expected_bodies_and_joints():
    model = mjcf.minimal_spec().compile()
    assert model.njnt == 2  # the fixture's freejoint and the lever's hinge
    assert model.nu == 1  # the lever's position actuator
    for name in ("floor", "plate", "bracket", "pin", "lever"):
        geom = model.geom(name)
        assert geom.id >= 0


def test_free_body_stays_put_for_one_second():
    model = mjcf.minimal_spec().compile()
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    start = data.xpos[model.body("fixture").id].copy()

    while data.time < 1.0:
        mujoco.mj_step(model, data)

    end = data.xpos[model.body("fixture").id].copy()
    drift = float(((end - start) ** 2).sum() ** 0.5)
    assert drift < 0.002  # metres: settling onto the floor, not a fall

    _, x, y, _ = data.qpos[3:7]
    tilt_deg = math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * (x * x + y * y)))))
    assert tilt_deg < 1.0
