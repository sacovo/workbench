"""Shared setup for the hub tests: throwaway git projects in tmp_path, isolated from the
developer's git configuration (no global identity, signing or hooks leak in)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from workbench.project import Project, find_project

WB = Path(sys.executable).with_name("wb")  # the console script installed in this environment

PYPROJECT = """\
[project]
name = "demo"

[tool.workbench]
manifest = "demo.manifest"
"""


def isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env or os.environ.copy()
    )
    return out.stdout


def git_quiet(cwd: Path, *args: str) -> str:
    """A git call that must not refresh (and so rewrite) the index as a side effect."""
    return git(cwd, *args, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """A git repository at `root` with a workbench pyproject, a small package and one commit."""
    root.mkdir(parents=True, exist_ok=True)
    content = {
        "pyproject.toml": PYPROJECT,
        "src/demo/__init__.py": "",
        "src/demo/manifest.py": "# importing this builds nothing\n",
        "src/demo/params.py": "BASE = {'wheel_width': 60}\n",
        ".gitignore": "build/\n",
        **(files or {}),
    }
    for rel, text in content.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    isolate_git(monkeypatch)
    return find_project(make_repo(tmp_path / "proj"))
