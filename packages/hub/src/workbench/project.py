"""Find a workbench project and its private state directory.

A project is the first directory, walking up from where `wb` runs, whose `pyproject.toml` has a
`[tool.workbench]` table. A `pyproject.toml` without the table is passed over (a nested package
inside a project is not a project of its own):

    [tool.workbench]
    manifest = "robot.manifest"   # required: the module the kernel imports; importing it builds nothing
    watch = ["src", "scripts"]    # optional: paths whose changes make the shown scene stale
    out = "build"                 # optional: the project's output directory, default "build"

Unknown keys are an error, so a misspelt key is not silently ignored. Paths are relative to the
project root and must stay inside it.

`<root>/.workbench/` holds what is never committed: `hub.json` (the running hub's pid, port and
token), the lock that serialises writers of `review/`, `blobs/` and `logs/`. It is created with its
own `.gitignore` ("*"), so it stays out of `git status`, build ids and snapshots even in a project
whose `.gitignore` does not list it; otherwise every `wb` call would change the build id and a
snapshot would commit the hub's token.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from workbench.errors import Busy, ProjectError

PYPROJECT = "pyproject.toml"
STATE_DIR = ".workbench"
CONFIG_KEYS = ("manifest", "watch", "out")
DEFAULT_OUT = "build"
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# How long a writer waits for another process (or the hub) to release the project lock before
# giving up with a message. Writers hold it for milliseconds; a long wait means something hangs.
LOCK_TIMEOUT = 10.0  # s
LOCK_POLL = 0.005  # s
HUB_CONNECT_TIMEOUT = 0.2  # s, for "is the hub listening"


@dataclass(frozen=True)
class Project:
    root: Path
    name: str
    manifest: str
    watch: tuple[str, ...]  # project-relative POSIX paths, as configured
    out: str  # project-relative POSIX path

    @property
    def pyproject(self) -> Path:
        return self.root / PYPROJECT

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def hub_json(self) -> Path:
        return self.state_dir / "hub.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    @property
    def blobs_dir(self) -> Path:
        return self.state_dir / "blobs"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def out_dir(self) -> Path:
        return self.root / self.out

    def ensure_state_dir(self) -> Path:
        """Create `.workbench/` with its own `.gitignore` if missing; return it."""
        d = self.state_dir
        if d.exists() and not d.is_dir():
            raise ProjectError(f"{d} exists and is not a directory")
        d.mkdir(exist_ok=True)
        ignore = d / ".gitignore"
        if not ignore.exists():
            atomic_write(ignore, "# Workbench state, written by wb: never committed.\n*\n")
        return d

    def ensure_blobs_dir(self) -> Path:
        self.ensure_state_dir()
        self.blobs_dir.mkdir(exist_ok=True)
        return self.blobs_dir

    def ensure_logs_dir(self) -> Path:
        self.ensure_state_dir()
        self.logs_dir.mkdir(exist_ok=True)
        return self.logs_dir

    def lock(self, exclusive: bool = True, timeout: float = LOCK_TIMEOUT):
        """Hold the project lock (`.workbench/lock`, fcntl.flock) for a block.

        Writers of `review/` take it exclusive, readers shared, so a reader never sees half an
        appended event and two processes never allocate the same id. Re-entrant within a thread
        (a nested request of the same or a weaker kind just counts); upgrading shared to
        exclusive is refused because flock cannot do it atomically.
        """
        return _lock(self, exclusive, timeout)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "root": str(self.root),
            "pyproject": str(self.pyproject),
            "manifest": self.manifest,
            "watch": list(self.watch),
            "out": self.out,
        }


def find_project(start: str | os.PathLike | None = None) -> Project:
    """The project containing `start` (default: the current directory)."""
    here = Path(os.getcwd() if start is None else start).resolve()
    if here.is_file():
        here = here.parent
    if not here.is_dir():
        raise ProjectError(f"{here} is not a directory")
    without_table: list[Path] = []
    for directory in (here, *here.parents):
        candidate = directory / PYPROJECT
        if not candidate.is_file():
            continue
        data = _read_toml(candidate)
        tool = data.get("tool")
        table = tool.get("workbench") if isinstance(tool, dict) else None
        if table is None:
            without_table.append(candidate)
            continue
        return _project_from(directory, data, table)
    seen = "".join(f"\n  {p} (no [tool.workbench] table)" for p in without_table)
    raise ProjectError(
        f"not a workbench project: no pyproject.toml with a [tool.workbench] table in {here} "
        f"or any parent directory.{(' Looked at:' + seen) if seen else ''}\n"
        "Add to the project's pyproject.toml:\n\n"
        "[tool.workbench]\n"
        'manifest = "<package>.manifest"'
    )


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{path} is not valid TOML: {exc}") from None


def _project_from(root: Path, data: dict, table: object) -> Project:
    where = f"[tool.workbench] in {root / PYPROJECT}"
    if not isinstance(table, dict):
        raise ProjectError(f"{where} must be a table")
    unknown = sorted(set(table) - set(CONFIG_KEYS))
    if unknown:
        raise ProjectError(f"{where}: unknown key(s) {', '.join(unknown)}; known keys: {', '.join(CONFIG_KEYS)}")
    if "manifest" not in table:
        raise ProjectError(f'{where}: "manifest" is required, e.g. manifest = "robot.manifest"')
    manifest = table["manifest"]
    if not isinstance(manifest, str) or not MODULE_RE.match(manifest):
        raise ProjectError(f"{where}: manifest must be a dotted module path like \"robot.manifest\", got {manifest!r}")
    watch_raw = table.get("watch", [])
    if not isinstance(watch_raw, list):
        raise ProjectError(f"{where}: watch must be a list of paths, got {type(watch_raw).__name__}")
    watch = tuple(_inside_path(p, f"{where}: watch entry") for p in watch_raw)
    out = _inside_path(table.get("out", DEFAULT_OUT), f"{where}: out")
    project_table = data.get("project")
    name = project_table.get("name") if isinstance(project_table, dict) else None
    if not isinstance(name, str) or not name:
        name = root.name
    return Project(root=root, name=name, manifest=manifest, watch=watch, out=out)


def _inside_path(value: object, where: str) -> str:
    """A non-empty relative POSIX path that stays inside the project, normalised."""
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{where} must be a non-empty string, got {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise ProjectError(f"{where} must be a path inside the project, got {value!r}")
    return str(p)


# ---------------------------------------------------------------------------------------------
# The running hub's address

@dataclass(frozen=True)
class HubInfo:
    pid: int
    port: int
    token: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def write_hub_info(project: Project, *, port: int, token: str, pid: int | None = None) -> Path:
    """Record the hub's pid, port and token (file mode 0600). For `wb serve`."""
    project.ensure_state_dir()
    data = {"pid": os.getpid() if pid is None else pid, "port": port, "token": token}
    atomic_write(project.hub_json, json.dumps(data) + "\n", mode=0o600)
    return project.hub_json


def clear_hub_info(project: Project) -> None:
    try:
        project.hub_json.unlink()
    except FileNotFoundError:
        pass


def read_hub_info(project: Project) -> HubInfo | None:
    """What `.workbench/hub.json` says, or None if there is none. Malformed is an error."""
    try:
        raw = project.hub_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    ok = (
        isinstance(data, dict)
        and set(data) == {"pid", "port", "token"}
        and all(type(data[k]) is int for k in ("pid", "port"))
        and isinstance(data["token"], str)
        and data["token"]
    )
    if not ok:
        raise ProjectError(f"{project.hub_json} is malformed; stop the hub and delete it")
    return HubInfo(pid=data["pid"], port=data["port"], token=data["token"])


def running_hub(project: Project) -> tuple[HubInfo | None, str]:
    """(hub, "") when a hub runs for this project, else (None, why not)."""
    try:
        info = read_hub_info(project)
    except ProjectError as exc:
        return None, str(exc)
    if info is None:
        return None, "no .workbench/hub.json"
    if not pid_alive(info.pid):
        return None, f"stale .workbench/hub.json: process {info.pid} is gone"
    try:
        with socket.create_connection(("127.0.0.1", info.port), timeout=HUB_CONNECT_TIMEOUT):
            pass
    except OSError:
        return None, f"process {info.pid} is not listening on port {info.port}"
    return info, ""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------------------------
# Files

def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace `path` with `text` so that readers see the old or the new file, never half."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------------------------
# The project lock

_held = threading.local()


class _Held:
    __slots__ = ("exclusive", "count")

    def __init__(self, exclusive: bool):
        self.exclusive = exclusive
        self.count = 1


@contextmanager
def _lock(project: Project, exclusive: bool, timeout: float) -> Iterator[None]:
    table: dict[str, _Held] = getattr(_held, "table", None) or {}
    _held.table = table
    key = str(project.lock_path)
    entry = table.get(key)
    if entry is not None:
        if exclusive and not entry.exclusive:
            raise RuntimeError("cannot upgrade a shared workbench lock to exclusive in the same thread")
        entry.count += 1
        try:
            yield
        finally:
            entry.count -= 1
        return
    project.ensure_state_dir()
    fd = os.open(project.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        _flock(fd, exclusive, timeout, project)
        table[key] = _Held(exclusive)
        try:
            yield
        finally:
            del table[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _flock(fd: int, exclusive: bool, timeout: float, project: Project) -> None:
    op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, op)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise Busy(
                    f"{project.lock_path} stayed locked for {timeout:.0f} s: another wb process or the hub "
                    "is holding it"
                ) from None
            time.sleep(LOCK_POLL)
