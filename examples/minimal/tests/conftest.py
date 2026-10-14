import os

# Every test runs headless: no display, EGL for MuJoCo. Set before mujoco is imported.
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ["MUJOCO_GL"] = "egl"
