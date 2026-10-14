"""Recorded body poses as three-cad-viewer animation tracks.

three-cad-viewer stores track values relative to the group's pose when the track is added
(scene/animation.ts): a position track holds `base_position + offset`, a quaternion track holds
`base_quaternion * offset_quaternion` (offset on the right). Body groups sit flat under the scene
root with the body's base world pose, so for a recorded world pose (p, q):

    offset   = p - base_p                    (mm)
    offset_q = conj(base_q) * q              (x, y, z, w)

The viewer then shows base_p + offset = p and base_q * conj(base_q) * q = q.
"""

from __future__ import annotations

import numpy as np

from sim_scene import MM, SimScene


def qmul_xyzw(a, b):
    ax, ay, az, aw = np.moveaxis(np.asarray(a), -1, 0)
    bx, by, bz, bw = np.moveaxis(np.asarray(b), -1, 0)
    return np.stack(
        [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ],
        axis=-1,
    )


def conj_xyzw(q):
    q = np.asarray(q, dtype=float)
    return q * np.array([-1, -1, -1, 1])


def wxyz_to_xyzw_arr(q):
    q = np.asarray(q)
    return q[..., [1, 2, 3, 0]]


def body_tracks(scene: SimScene, times, xpos, xquat, bodies=None, moving_only=True, tol=1e-12):
    """[(path, action, times, values)] for every body group of the scene.

    `times` are rebased to start at 0. With `moving_only`, a body whose pose never leaves its base
    pose gets no track (the world body).
    """
    times = np.asarray(times, dtype=float)
    t = times - times[0]
    out = []
    for b, path in scene.body_paths.items():
        if bodies is not None and b not in bodies:
            continue
        p = np.asarray(xpos)[:, b] * MM
        q = wxyz_to_xyzw_arr(np.asarray(xquat)[:, b])
        base_p = np.asarray(scene.base_pos_mm[b])
        base_q = np.asarray(scene.base_quat_xyzw[b])
        dp = p - base_p
        dq = qmul_xyzw(conj_xyzw(base_q)[None, :], q)
        # keep consecutive quaternions in one hemisphere (the viewer slerps the short way anyway)
        for i in range(1, len(dq)):
            if np.dot(dq[i], dq[i - 1]) < 0:
                dq[i] = -dq[i]
        if moving_only and np.abs(dp).max() < tol and np.abs(np.abs(dq[:, 3]) - 1).max() < tol:
            continue
        out.append((path, "t", t.tolist(), dp.tolist()))
        out.append((path, "q", t.tolist(), dq.tolist()))
    return out


def animation_message(tracks, speed=1.0):
    """The message `Animation.animate()` sends (ocp_viewer_core/animation.py), built directly:
    that helper only accepts paths of the last `show`, and this scene is not sent by `show`."""
    return {"data": [list(tr) for tr in tracks], "type": "animation", "config": {"speed": speed}}
