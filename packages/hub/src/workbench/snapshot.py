"""Snapshots: the working tree as a commit object under `refs/wb/snap/<build id>`.

A reviewer, a what-if run or an old item needs the exact code a build id names, even after the
working tree moved on. A snapshot stores it without touching anything the owner or the agent works
with: it builds a tree in a temporary index (`GIT_INDEX_FILE` in a temp dir: read-tree HEAD,
add -A scoped the same way as the build id, write-tree), writes a commit whose parent is HEAD
(commit-tree -p HEAD) and points `refs/wb/snap/<build id>` at it. The real index, HEAD, the branch
and the working tree stay as they were; only new objects and the one ref are written.

The snapshot holds what the build id hashes: HEAD's tree, with the working changes of the
project's own in-scope, non-excluded paths applied on top (`workbench.buildid.project_scope`);
excluded (`review/`, `.workbench/`) and out-of-scope paths keep HEAD's content. `refs/wb/snap/<id>^`
is always the HEAD the id was taken on, so `git diff refs/wb/snap/<id>^ refs/wb/snap/<id>` is the
uncommitted change, restricted to that same scope. Restore with
`git worktree add --detach <dir> refs/wb/snap/<id>`.

Snapshot commits use a fixed identity ("wb snapshot <wb@localhost>") and are never signed, so they
are recognisable, work without a configured git identity and never prompt for a key.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from workbench.buildid import build_state, git, git_env, project_scope
from workbench.errors import Conflict, GitError

SNAP_PREFIX = "refs/wb/snap/"
IDENTITY = {
    "GIT_AUTHOR_NAME": "wb snapshot",
    "GIT_AUTHOR_EMAIL": "wb@localhost",
    "GIT_COMMITTER_NAME": "wb snapshot",
    "GIT_COMMITTER_EMAIL": "wb@localhost",
}
ATTEMPTS = 3  # a working tree that changes while we read it gets this many tries


def snapshot(path: str | os.PathLike) -> dict:
    """Snapshot the project at `path`; reuse the ref if this state has one already."""
    for _ in range(ATTEMPTS):
        before = build_state(path)
        tree = working_tree(path)
        after = build_state(path)
        if before.id == after.id and before.head == after.head:
            break
    else:
        raise Conflict(f"the working tree kept changing during {ATTEMPTS} snapshot attempts; try again")

    top = before.repo
    ref = SNAP_PREFIX + before.id
    existing = _resolve(top, ref)
    if existing is None:
        message = f"wb snapshot {before.id}\n\nWorking tree of build {before.id} on top of {before.short}.\n"
        commit = (
            git(
                ["commit-tree", tree, "-p", before.head, "--no-gpg-sign", "-m", message],
                cwd=top,
                env=git_env(**IDENTITY),
            )
            .decode()
            .strip()
        )
        try:
            # The empty old value makes this "create, never overwrite": two snapshots racing for the
            # same id leave the first one in place, and the second compares trees below.
            git(["update-ref", "-m", "wb snapshot", ref, commit, ""], cwd=top)
            return _result(before, ref, commit, tree, top, created=True)
        except GitError:
            existing = _resolve(top, ref)
            if existing is None:
                raise
    existing_tree = git(["rev-parse", f"{existing}^{{tree}}"], cwd=top).decode().strip()
    if existing_tree != tree:
        raise Conflict(
            f"{ref} already points at {existing} with tree {existing_tree}, but the working tree is {tree}; "
            "two different states share a build id, or the ref was changed by hand"
        )
    return _result(before, ref, existing, tree, top, created=False)


def working_tree(path: str | os.PathLike) -> str:
    """The tree object id of HEAD with the project's own in-scope working changes applied (tracked
    and untracked, not ignored; excluded and out-of-scope paths keep HEAD's content), via a temp
    index."""
    top, pathspec = project_scope(path)
    with tempfile.TemporaryDirectory(prefix="wb-snapshot-") as tmp:
        env = git_env(GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(["read-tree", "HEAD"], cwd=top, env=env)
        git(["add", "-A", "--", *pathspec], cwd=top, env=env)
        return git(["write-tree"], cwd=top, env=env).decode().strip()


def _resolve(top: Path, ref: str) -> str | None:
    try:
        return git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=top).decode().strip()
    except GitError:
        return None


def _result(state, ref: str, commit: str, tree: str, top: Path, created: bool) -> dict:
    return {
        "build": state.id,
        "ref": ref,
        "commit": commit,
        "tree": tree,
        "head": state.head,
        "repo": str(top),
        "created": created,
    }
