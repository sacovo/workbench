"""Build ids for clean, dirty and untracked states, and snapshots that leave the index, HEAD, the
branch and the working tree alone while their tree equals the working tree."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from hub_helpers import git, git_quiet, isolate_git, make_repo, project  # noqa: F401  (project: fixture)
from workbench.buildid import build_id, build_state
from workbench.errors import GitError
from workbench.snapshot import snapshot

DIRTY = re.compile(r"^[0-9a-f]{7}\+[0-9a-f]{8}$")


def test_a_clean_tree_is_the_short_head(project):
    head = git(project.root, "rev-parse", "--short=7", "HEAD").strip()
    assert build_id(project.root) == head
    assert build_id(project.root / "src" / "demo") == head  # from anywhere inside the repository


def test_dirty_id_follows_content_and_mode_not_staging_or_timestamps(project):
    root = project.root
    clean = build_id(root)
    params = root / "src/demo/params.py"
    original = params.read_text()

    os.utime(params, (1_000_000_000, 1_000_000_000))
    assert build_id(root) == clean  # touched, not changed

    params.write_text(original + "LIFT = 2\n")
    dirty = build_id(root)
    assert DIRTY.match(dirty) and dirty.startswith(clean + "+")
    assert build_id(root) == dirty  # same state, same id
    git(root, "add", "src/demo/params.py")
    assert build_id(root) == dirty  # staging is not a change of the working tree

    params.write_text(original + "LIFT = 3\n")
    other = build_id(root)
    params.chmod(0o755)
    executable = build_id(root)
    assert len({dirty, other, executable}) == 3

    params.chmod(0o644)
    params.write_text(original)
    assert build_id(root) == clean  # back to HEAD's content, although the index still differs

    params.unlink()
    assert build_id(root).startswith(clean + "+")  # a deletion is a change


def test_untracked_files_count_unless_ignored(project):
    root = project.root
    clean = build_id(root)
    (root / "build").mkdir()
    (root / "build" / "plate.dxf").write_text("ignored output")
    assert build_id(root) == clean

    notes = root / "notes" / "draft.md"
    notes.parent.mkdir()
    notes.write_text("draft 1")
    first = build_id(root)
    assert DIRTY.match(first)
    notes.write_text("draft 2")
    assert build_id(root) not in (clean, first)
    notes.unlink()
    assert build_id(root) == clean


def test_outside_a_repository_or_before_a_commit_is_an_error(tmp_path, monkeypatch):
    isolate_git(monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # tmp_path may sit inside a repo
    with pytest.raises(GitError, match="not inside a git repository"):
        build_id(tmp_path)
    git(tmp_path, "init", "-q")
    with pytest.raises(GitError, match="no commit yet"):
        build_id(tmp_path)


def _files_on_disk(root):
    """Every file git does not ignore, with its mode and bytes, read from the file system."""
    ignored = set(git_quiet(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"))
    found = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not (directory == str(root) and d == ".git")]
        for name in files:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            if rel in ignored:
                continue
            mode = "100755" if os.stat(path).st_mode & 0o100 else "100644"
            with open(path, "rb") as f:
                found[rel] = (mode, f.read())
    return found


def _files_in_commit(root, commit):
    found = {}
    for entry in git_quiet(root, "ls-tree", "-r", "-z", commit).split("\0"):
        if not entry:
            continue
        meta, rel = entry.split("\t", 1)
        mode, _kind, sha = meta.split()
        blob = subprocess.run(["git", "cat-file", "blob", sha], cwd=root, capture_output=True, check=True).stdout
        found[rel] = (mode, blob)
    return found


def _repo_state(root):
    return {
        "index": (root / ".git" / "index").read_bytes(),
        "HEAD": (root / ".git" / "HEAD").read_text(),
        "head commit": git_quiet(root, "rev-parse", "HEAD"),
        "branch": git_quiet(root, "rev-parse", "refs/heads/main"),
        "status": git_quiet(root, "status", "--porcelain=v1", "--untracked-files=all"),
        "files": _files_on_disk(root),
        "mtimes": {p: os.stat(root / p).st_mtime_ns for p in _files_on_disk(root)},
    }


def test_snapshot_is_the_working_tree_and_touches_nothing_else(project):
    root = project.root
    head = git(root, "rev-parse", "HEAD").strip()
    # Every kind of change at once.
    (root / "src/demo/params.py").write_text("BASE = {'wheel_width': 62}\n")  # modified
    (root / "src/demo/manifest.py").unlink()  # deleted
    (root / "staged.txt").write_text("staged\n")
    git(root, "add", "staged.txt")  # new and staged
    (root / "notes/deep").mkdir(parents=True)
    (root / "notes/deep/todo.md").write_text("untracked\n")  # untracked in a subdirectory
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "run.sh").chmod(0o755)  # untracked, executable
    (root / "build").mkdir()
    (root / "build/out.bin").write_bytes(b"\0ignored")  # ignored
    os.utime(root / "src/demo/__init__.py", (1_000_000_000, 1_000_000_000))  # touched: a refresh would rewrite the index

    before = _repo_state(root)
    result = snapshot(root)
    assert _repo_state(root) == before

    assert result["created"] is True
    assert result["ref"] == f"refs/wb/snap/{build_id(root)}"
    assert DIRTY.match(result["build"])
    assert git_quiet(root, "rev-parse", result["ref"]).strip() == result["commit"]
    assert git_quiet(root, "rev-parse", f"{result['commit']}^").strip() == head
    in_commit = _files_in_commit(root, result["commit"])
    assert in_commit == before["files"]
    assert "run.sh" in in_commit and "notes/deep/todo.md" in in_commit and "staged.txt" in in_commit
    assert "build/out.bin" not in in_commit and "src/demo/manifest.py" not in in_commit

    again = snapshot(root)  # same state: the ref is reused, not rewritten
    assert (again["created"], again["commit"], again["ref"]) == (False, result["commit"], result["ref"])

    # Control: the setup was able to show an index rewrite; a plain `git status` refreshes it.
    git(root, "status")
    assert (root / ".git" / "index").read_bytes() != before["index"]


def test_snapshot_of_a_clean_tree_has_the_head_tree(project):
    root = project.root
    result = snapshot(root)
    assert result["build"] == git(root, "rev-parse", "--short=7", "HEAD").strip()
    assert result["tree"] == git(root, "rev-parse", "HEAD^{tree}").strip()
    assert git(root, "rev-parse", f"{result['ref']}^").strip() == git(root, "rev-parse", "HEAD").strip()


def test_snapshot_works_without_a_git_identity(tmp_path, monkeypatch):
    # git commit-tree refuses to run without one (measured, exit 128); snapshots bring their own.
    isolate_git(monkeypatch)
    root = make_repo(tmp_path / "proj")
    git(root, "config", "--unset", "user.name")
    git(root, "config", "--unset", "user.email")
    (root / "new.txt").write_text("x\n")
    result = snapshot(root)
    author = git(root, "log", "-1", "--format=%an <%ae>", result["commit"]).strip()
    assert author == "wb snapshot <wb@localhost>"
    assert build_state(root).dirty
