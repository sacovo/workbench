"""Where scripts write their output.

Scripts write into `build_dir()`, not a hardcoded path, so an agent's what-if
run or a reviewer working in a copy of the repo can point output elsewhere
(`WB_OUT`) without the two runs clobbering each other's `build/` directory.
"""

import os
from pathlib import Path

# src/minimal/paths.py -> src/minimal -> src -> examples/minimal (this project's root)
ROOT = Path(__file__).resolve().parents[2]


def build_dir() -> Path:
    """The directory scripts should write their output into, created if it does not exist."""
    out = Path(os.environ["WB_OUT"]) if os.environ.get("WB_OUT") else ROOT / "build"
    out.mkdir(parents=True, exist_ok=True)
    return out
