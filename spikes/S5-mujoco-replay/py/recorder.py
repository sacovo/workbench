"""Record body poses from any simulation that steps through `mujoco.mj_step`.

    with recording(model, rate_hz=60) as rec:
        stability.arm_swing(model, ...)
    run = rec.longest()

The wrapper replaces the module attribute `mujoco.mj_step` for the duration of the block (the
robot's code calls it as `mujoco.mj_step(model, data)`), and restores it afterwards.

Time stamps. After `mj_step`, `data.xpos` / `data.xquat` were computed by the forward pass at the
start of that step, from the state *before* integration, while `data.time` and `data.qpos` have
already advanced by one timestep. A sample is therefore stamped with the time before the step
and stored with the `qpos` from before the step, so time, qpos and poses describe one state.
Nothing extra is computed and the simulation is not touched: the recorded run is bit-identical
to an unrecorded one (checked in record_runs.py).

A run is one `MjData` from its first step until its time goes backwards (mj_resetData); each run
is sampled on its own 60 Hz grid of simulated time, starting at its first step.

Because samples are taken at the start of a step, the state after a run's last step is not among
them. When the block exits, each run gets one more sample for that final state, computed with
mj_kinematics on a scratch MjData (the simulation's own data is only read).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import mujoco
import numpy as np


@dataclass
class Run:
    data_id: int
    rate_hz: float
    times: list = field(default_factory=list)
    xpos: list = field(default_factory=list)
    xquat: list = field(default_factory=list)
    qpos: list = field(default_factory=list)
    steps: int = 0
    next_sample: float | None = None
    last_time: float = -np.inf
    end_time: float | None = None  # data.time after the last step
    end_qpos: np.ndarray | None = None  # data.qpos after the last step
    end_mocap: tuple | None = None
    final_sample_added: bool = False

    def arrays(self):
        return {
            "times": np.asarray(self.times),
            "xpos": np.asarray(self.xpos),
            "xquat": np.asarray(self.xquat),
            "qpos": np.asarray(self.qpos),
        }

    @property
    def duration(self):
        return self.times[-1] - self.times[0] if self.times else 0.0

    def nbytes(self):
        a = self.arrays()
        return {k: int(v.nbytes) for k, v in a.items()}


class Recording:
    def __init__(self, model, rate_hz):
        self.model = model
        self.rate_hz = rate_hz
        self.runs: list[Run] = []
        self._current: dict[int, Run] = {}
        self.foreign_steps = 0  # steps of other models, passed through unrecorded

    def _run_for(self, data, t):
        run = self._current.get(id(data))
        if run is None or t < run.last_time - 1e-12:
            run = Run(id(data), self.rate_hz)
            self._current[id(data)] = run
            self.runs.append(run)
        return run

    def step(self, real, m, d, nstep):
        if m is not self.model:
            self.foreign_steps += 1
            return real(m, d, nstep)
        t_before = d.time
        run = self._run_for(d, t_before)
        q_before = d.qpos.copy()
        real(m, d, nstep)
        run.steps += nstep
        run.last_time = d.time
        run.end_time = d.time
        run.end_qpos = d.qpos.copy()
        run.end_mocap = (d.mocap_pos.copy(), d.mocap_quat.copy())
        if nstep != 1:
            # with several substeps the poses belong to the start of the last one
            t_before = d.time - m.opt.timestep
            q_before = None
        if run.next_sample is None:
            run.next_sample = t_before
        if t_before >= run.next_sample - 1e-9:
            run.times.append(t_before)
            run.xpos.append(d.xpos.copy())
            run.xquat.append(d.xquat.copy())
            run.qpos.append(q_before if q_before is not None else np.full(m.nq, np.nan))
            period = 1.0 / self.rate_hz
            while run.next_sample <= t_before + 1e-9:
                run.next_sample += period

    def finish(self):
        """Add each run's final state (after its last step) as one more sample."""
        scratch = mujoco.MjData(self.model)
        for run in self.runs:
            if run.final_sample_added or run.end_qpos is None or (run.times and run.end_time <= run.times[-1] + 1e-12):
                continue
            scratch.qpos[:] = run.end_qpos
            scratch.mocap_pos[:], scratch.mocap_quat[:] = run.end_mocap
            mujoco.mj_kinematics(self.model, scratch)
            run.times.append(run.end_time)
            run.xpos.append(scratch.xpos.copy())
            run.xquat.append(scratch.xquat.copy())
            run.qpos.append(run.end_qpos.copy())
            run.final_sample_added = True

    def longest(self) -> Run:
        return max(self.runs, key=lambda r: r.duration)


@contextlib.contextmanager
def recording(model, rate_hz=60.0):
    real = mujoco.mj_step
    if getattr(real, "_s5_recorder", False):
        raise RuntimeError("mujoco.mj_step is already wrapped")
    rec = Recording(model, rate_hz)

    def mj_step(m, d, nstep=1):
        return rec.step(real, m, d, nstep)

    mj_step._s5_recorder = True
    mujoco.mj_step = mj_step
    try:
        yield rec
    finally:
        mujoco.mj_step = real
        rec.finish()
