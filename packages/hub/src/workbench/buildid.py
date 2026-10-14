"""Build ids: which code state an output or a review event was made from.

A build id is the short HEAD commit ("5367af0") when the working tree matches HEAD, and the short
commit plus "+" and 8 hex digits of a hash of the dirty state ("5367af0+1c2d3e4f") when it does
not. The commit part is always the repository's HEAD, even when the project is nested inside a
larger repository. The dirty state is scoped to the project directory (the directory holding the
`pyproject.toml` with the `[tool.workbench]` table, as a pathspec relative to the git top level;
see `project_scope`), excluding the project's own `review/` and `.workbench/`: review items are
discussion and `.workbench/` is private state, neither is design, so writing a comment or an
answer must not change the id. Within that scope it is:

- the diff of tracked files against HEAD, as git sees it (`git diff HEAD`, renames off): for each
  changed path its new mode and the hash of its new content, or that it was deleted;
- every untracked file that is not ignored: path, mode and content hash.

The hash is over this list, not over patch text, so diff settings (algorithm, context, prefixes)
cannot change an id. Staging changes nothing: the id describes the working tree. Touching a file
without changing it does not make the tree dirty. A change elsewhere in the same repository (a
sibling project, or the hub and kernel source above it) does not change it either.

Read only. `git diff` refreshes and rewrites the index as a side effect when it meets files whose
timestamps changed, even with GIT_OPTIONAL_LOCKS=0 (measured with git 2.55), so it runs here on a
throwaway copy of the index. Nothing writes to the real index, the refs or the object database.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from workbench.errors import GitError

SHORT_LEN = 7
DIRTY_LEN = 8
BUILD_ID_RE = re.compile(r"^[0-9a-f]{7,64}(\+[0-9a-f]{8})?$")

# Variables that would point git at another repository, index or object store than the one that
# contains the project. Git sets some of them inside hooks; a `wbench` call from a hook must not follow.
_REPO_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def git_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # keeps `git status`-like refreshes from writing the index
    env["LC_ALL"] = "C"
    env.update(extra)
    return env


def git(args: list[str], cwd: str | os.PathLike, env: dict[str, str] | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, env=env or git_env(), capture_output=True)
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {message}")
    return proc.stdout


@dataclass(frozen=True)
class BuildState:
    id: str
    repo: Path  # top level of the repository
    head: str  # full HEAD commit id
    short: str
    changed: int  # tracked paths whose content or mode differs from HEAD
    untracked: int  # untracked files that are not ignored

    @property
    def dirty(self) -> bool:
        return "+" in self.id


def repo_root(path: str | os.PathLike) -> Path:
    try:
        out = git(["rev-parse", "--show-toplevel"], cwd=path)
    except GitError:
        raise GitError(f"{path} is not inside a git repository") from None
    return Path(os.fsdecode(out.rstrip(b"\n")))


# A project's own review items and private state are discussion, not design (module docstring):
# excluded from the dirty hash and from snapshots. Matches workbench.review.store.REVIEW_DIR and
# workbench.project.STATE_DIR; kept as literals here rather than imported, since review.store
# already imports this module and project.py is lower-level than either.
_EXCLUDED_FROM_SCOPE = ("review", ".workbench")


def project_scope(project_root: str | os.PathLike) -> tuple[Path, list[str]]:
    """The repository top level, and the pathspec that limits a dirty-state `git diff`, `git
    ls-files` or `git add` to `project_root`'s own files: everything under it except
    `_EXCLUDED_FROM_SCOPE`. `project_root` is `top` itself or a directory inside it."""
    top = repo_root(project_root)
    rel = Path(project_root).resolve().relative_to(top).as_posix()

    def _child(name: str) -> str:
        return name if rel == "." else f"{rel}/{name}"

    pathspec = [f":(literal){rel}"] + [f":(exclude,literal){_child(name)}" for name in _EXCLUDED_FROM_SCOPE]
    return top, pathspec


def head_commit(top: str | os.PathLike) -> str:
    try:
        return git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=top).decode().strip()
    except GitError:
        raise GitError(f"{top} has no commit yet; a build id needs one") from None


def build_state(path: str | os.PathLike) -> BuildState:
    """The build state of the project at `path` (its own files only; see the module docstring)."""
    top, pathspec = project_scope(path)
    head = head_commit(top)
    short = git(["rev-parse", f"--short={SHORT_LEN}", head], cwd=top).decode().strip()
    records, changed, untracked = _dirty_records(top, pathspec)
    if not records:
        return BuildState(id=short, repo=top, head=head, short=short, changed=0, untracked=0)
    h = hashlib.sha256()
    for kind, rel, mode, digest in sorted(records, key=lambda r: (r[1], r[0])):
        h.update(kind + b"\0" + mode + b"\0" + rel + b"\0" + digest)
    return BuildState(
        id=f"{short}+{h.hexdigest()[:DIRTY_LEN]}",
        repo=top,
        head=head,
        short=short,
        changed=changed,
        untracked=untracked,
    )


def build_id(path: str | os.PathLike) -> str:
    """The build id of the project at `path` (see the module docstring). Raises GitError outside a
    git repository or before its first commit."""
    return build_state(path).id


def _dirty_records(top: Path, pathspec: list[str]) -> tuple[list[tuple[bytes, bytes, bytes, bytes]], int, int]:
    """(kind, path, mode, content digest) for each tracked change and each untracked file inside
    `pathspec` (from `project_scope`)."""
    index = Path(os.fsdecode(git(["rev-parse", "--git-path", "index"], cwd=top).rstrip(b"\n")))
    if not index.is_absolute():
        index = top / index
    with tempfile.TemporaryDirectory(prefix="wb-buildid-") as tmp:
        scratch_index = Path(tmp) / "index"
        try:
            shutil.copyfile(index, scratch_index)
        except FileNotFoundError:
            pass  # no index yet: git reads a missing index as empty, here as there
        env = git_env(GIT_INDEX_FILE=str(scratch_index))
        raw = git(
            ["diff", "HEAD", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "--", *pathspec],
            cwd=top,
            env=env,
        )
        others = git(["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec], cwd=top, env=env)

    top_b = os.fsencode(top)
    records = []
    changed = 0
    fields = raw.split(b"\0")
    i = 0
    while i + 1 < len(fields) and fields[i]:
        meta, rel = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(b":"):
            raise GitError(f"unexpected `git diff --raw` output: {meta[:80]!r}")
        _old_mode, new_mode, _old_sha, new_sha, _status = meta[1:].split(b" ")[:5]
        changed += 1
        if new_mode == b"000000":
            records.append((b"deleted", rel, new_mode, b""))
        elif new_mode == b"160000":  # submodule: its checked-out commit stands for its content
            records.append((b"tracked", rel, new_mode, new_sha))
        else:
            records.append((b"tracked", rel, new_mode, _digest(os.path.join(top_b, rel), new_mode)))

    untracked = 0
    for rel in others.split(b"\0"):
        if not rel:
            continue
        untracked += 1
        full = os.path.join(top_b, rel)
        if rel.endswith(b"/"):  # an untracked nested repository: git lists only its directory
            records.append((b"untracked", rel, b"040000", b""))
            continue
        records.append((b"untracked", rel, *_mode_and_digest(full)))
    return records, changed, untracked


def _mode_and_digest(full: bytes) -> tuple[bytes, bytes]:
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return b"000000", b""  # gone since git listed it
    if stat.S_ISLNK(st.st_mode):
        mode = b"120000"
    elif st.st_mode & stat.S_IXUSR:
        mode = b"100755"
    else:
        mode = b"100644"
    return mode, _digest(full, mode)


def _digest(full: bytes, mode: bytes) -> bytes:
    try:
        if mode == b"120000":
            return hashlib.sha256(os.readlink(full)).digest()
        h = hashlib.sha256()
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.digest()
    except FileNotFoundError:
        return b"gone"
