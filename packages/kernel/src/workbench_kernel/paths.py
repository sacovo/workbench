"""Where the kernel finds a project's manifest and where it writes its blobs."""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from pathlib import Path

from workbench_kernel.api import ManifestError


def manifest_module(project_root: Path) -> str:
    """The module named in `[tool.workbench] manifest` of the project's pyproject.toml."""
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        raise ManifestError(f"no pyproject.toml in {project_root}")
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    name = data.get("tool", {}).get("workbench", {}).get("manifest")
    if not isinstance(name, str) or not name:
        raise ManifestError(f'{pyproject} has no [tool.workbench] manifest = "<module>"')
    return name


def blobs_dir(project_root: Path) -> Path:
    return project_root / ".workbench" / "blobs"


def write_blob(project_root: Path, payload: bytes, kind: str) -> dict:
    """Store `payload` under its sha256 and describe it. Written to a temporary file and renamed,
    so a reader never sees half a blob; an existing blob with the same hash is left as it is."""
    sha = hashlib.sha256(payload).hexdigest()
    directory = blobs_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.json"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{sha[:12]}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return {"kind": kind, "sha": sha, "path": str(path), "bytes": len(payload)}
