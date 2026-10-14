"""The hub's state for one project: kernel generations, the pages, the shown scene and the pick.

Kernel generations. A kernel is a process in the project's environment (kernel.py). The hub
starts one at launch so the manifest is known. `show` builds there when nothing changed, or in a
NEW kernel when the code changed (or a rebuild is asked for), so code changes are picked up:

    show(assembly, args)
      serving kernel alive, code unchanged, same assembly on screen  ->  `update` in place (moved
                                                                         and changed parts only)
      serving kernel alive, code unchanged, other assembly          ->  `build` in place
      code changed, rebuild asked, or no live kernel                ->  new kernel: hello, build;
                                                                         then swap
    The old kernel keeps answering picks and measure requests while the new one builds, and is
    stopped once no page shows its scene any more (at most RETIRE_GRACE later). If the new kernel
    fails or dies, the last good scene stays and the failure is the kernel state's error.

"Code changed" compares a signature (names, sizes and modification times) of the files under the
project's `watch` paths, or the manifest's top-level package when none are configured, plus
pyproject.toml and uv.lock, with the one taken when the kernel was started. It is also what marks
the shown scene stale. The build id is not used for this: it covers every design file of the
project (docs and tests too), while `watch` names what a scene is built from.

Kernel state: starting | ready | building | crashed. `crashed` means the serving kernel is gone;
the last error (a crash, or a build that failed) is kept with the state until a build succeeds.

Pages. A page that connects gets the settings, the review state and the current scene: the last
full data message plus the incremental updates since, in order, so a model shown before any page
existed is not lost. The page reports which scene it shows ("shown"), its viewer status (camera,
tree, tools) and picks; measure-tool messages are relayed to the kernel's stock backend and the
answer goes back to the page that asked.

Picks. The stock select tool reports face indices only, so picks come from the measure tools
(`selectedShapeIDs`, full sub-shape paths) and from a double-click (`lastPick`, a part), as spike
S1 proved. The hub resolves the id in the kernel that holds the scene and keeps the result as the
owner's selection; viewer indices are not part of what it keeps.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from workbench.buildid import build_id
from workbench.errors import Conflict, GitError, HubNotRunning, InvalidInput, NotFound, WorkbenchError
from workbench.hub import review_watch
from workbench.hub.kernel import KernelDied, KernelError, KernelProcess, KernelTimeout
from workbench.hub.kernel import kernel_command as default_kernel_command
from workbench.project import Project
from workbench.review import ReviewStore, make_by, now
from workbench.review.schema import check_camera

log = logging.getLogger("workbench.hub")

HELLO_TIMEOUT = 300.0  # s: the first `uv run` of a project may have to sync its environment
BUILD_TIMEOUT = 600.0  # s: one build or update
PICK_TIMEOUT = 60.0  # s: a pick or a measure request may wait behind an update in the same kernel
RETIRE_GRACE = 30.0  # s an old kernel keeps answering after a swap
REVIEW_POLL = 0.5  # s
CODE_POLL = 2.0  # s
MAX_IMAGE_BYTES = 16 * 2**20

# Keys of a viewer status message that report an event rather than a state (ocp-viewer-core's
# notify.js EVENT_KEYS): never kept in the status a build is told about.
EVENT_KEYS = frozenset({"selectedShapeIDs", "selected", "lastPick", "activeTool", "relative_time", "copiedPoint"})
BACKEND_KEYS = ("activeTool", "selectedShapeIDs")  # what the stock measurement backend reads

# What the stock viewer needs before its first model (the stock standalone viewer's resolved
# defaults, as the kernel's host.SETTINGS and spike S1 use them).
PAGE_SETTINGS = {
    "theme": "light",
    "tree_width": 240,
    "glass": True,
    "tools": True,
    "up": "Z",
    "control": "trackball",
    "modifier_keys": {"shift": "shiftKey", "ctrl": "ctrlKey", "meta": "altKey", "alt": "metaKey"},
}

_RUNNING: dict[Path, Hub] = {}
_RUNNING_LOCK = threading.Lock()


def running_instance(project: Project) -> Hub:
    """The hub running in this process for `project` (for the operations that need one)."""
    with _RUNNING_LOCK:
        hub = _RUNNING.get(project.root)
    if hub is None:
        raise HubNotRunning("hub not running in this process: start it with `wb serve`")
    return hub


# ---------------------------------------------------------------------------------------------
# Code signature


def code_paths(project: Project) -> list[Path]:
    if project.watch:
        paths = [project.root / p for p in project.watch]
    else:
        top = project.manifest.split(".")[0]
        paths = []
        for base in ("src", "."):
            for candidate in (project.root / base / top, project.root / base / f"{top}.py"):
                if candidate.exists() and not paths:
                    paths.append(candidate)
    return [*paths, project.pyproject, project.root / "uv.lock"]


def code_signature(project: Project) -> str:
    """Names, sizes and modification times of the files that make a scene stale when they change."""
    h = hashlib.sha256()
    for path in code_paths(project):
        if path.is_dir():
            files = []
            for directory, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
                files += [Path(directory) / n for n in names if not n.endswith((".pyc", ".pyo")) and not n.startswith(".")]
        else:
            files = [path]
        for f in sorted(files):
            try:
                st = f.stat()
            except OSError:
                h.update(f"{f}\0missing\n".encode())
                continue
            h.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# Pages


class Page:
    """One connected page: a send queue drained by one writer, and what it reports it shows."""

    _ids = 0

    def __init__(self, websocket):
        Page._ids += 1
        self.id = Page._ids
        self.ws = websocket
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.scene: str | None = None  # the scene id the page last reported as shown
        self.sent_bytes = 0

    def send(self, message: dict) -> int:
        text = json.dumps(message, separators=(",", ":"), allow_nan=False)
        self.queue.put_nowait(text)
        return len(text.encode())

    async def writer(self) -> None:
        while (text := await self.queue.get()) is not None:
            await self.ws.send_text(text)
            self.sent_bytes += len(text)

    def close(self) -> None:
        self.queue.put_nowait(None)


@dataclass
class Shown:
    """The scene on screen: the last full data message and the incremental updates since."""

    base: dict  # wb_scene message
    generation: int
    signature: str | None
    updates: list[dict] = field(default_factory=list)  # wb_update messages, in order

    @property
    def scene(self) -> str:
        return self.updates[-1]["scene"] if self.updates else self.base["scene"]

    @property
    def assembly(self) -> str:
        return self.base["assembly"]

    @property
    def args(self) -> dict:
        return self.updates[-1]["args"] if self.updates else self.base["args"]

    @property
    def build(self) -> str | None:
        return self.updates[-1]["build"] if self.updates else self.base["build"]


def _slim_blob(blob: dict | None) -> dict | None:
    return None if blob is None else {"sha": blob["sha"], "bytes": blob["bytes"], "kind": blob["kind"]}


def _error(kind: str, exc: BaseException, generation: int | None, **extra) -> dict:
    out = {"kind": kind, "message": str(exc), "generation": generation, "at": now(), **extra}
    if isinstance(exc, KernelError):
        out["type"] = exc.type
        out["traceback"] = exc.traceback
    return out


# ---------------------------------------------------------------------------------------------
# The hub


class Hub:
    def __init__(
        self,
        project: Project,
        token: str,
        port: int,
        *,
        kernel_command: list[str] | None = None,
        auto_show: bool = True,
        review_poll: float = REVIEW_POLL,
        code_poll: float = CODE_POLL,
        retire_grace: float = RETIRE_GRACE,
    ):
        self.project = project
        self.token = token
        self.port = port
        self.kernel_command = kernel_command or default_kernel_command(project.root)
        self.auto_show = auto_show
        self.review_poll = review_poll
        self.code_poll = code_poll
        self.retire_grace = retire_grace
        self.loop: asyncio.AbstractEventLoop | None = None

        self.state = "starting"
        self.error: dict | None = None
        self.activity: dict | None = None  # what is being built, while building
        self.generation = 0
        self.kernel: KernelProcess | None = None  # the serving kernel
        self.pending: KernelProcess | None = None  # a new kernel being built
        self.retired: list[KernelProcess] = []  # old kernels kept until no page shows their scene
        self.manifest: dict | None = None
        self.scene_generation: dict[str, int] = {}  # scene id -> generation of the kernel that holds it
        self.shown: Shown | None = None
        self.scenes: dict[str, dict] = {}  # last scene summary per assembly
        self.pages: set[Page] = set()
        self.page_status: dict = {}
        self.active_tool: str | None = None
        self.selection: dict | None = None
        self._pick_seq = 0
        self._pick_task: asyncio.Task | None = None
        self.build_id: str | None = None
        self.stale = False
        self.review: dict = {"type": "wb_review", "items": [], "unreadable": [], "views": [], "pins": []}
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._show_lock: asyncio.Lock | None = None
        self._first_kernel: asyncio.Event | None = None
        self._review_poke: asyncio.Event | None = None
        self._auto_showing = False
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._show_lock = asyncio.Lock()
        self._first_kernel = asyncio.Event()
        self._review_poke = asyncio.Event()
        with _RUNNING_LOCK:
            _RUNNING[self.project.root] = self
        self.project.ensure_logs_dir()
        self.build_id = await asyncio.to_thread(self._current_build_id)
        self.review = await asyncio.to_thread(review_watch.snapshot, self.project)
        self._tasks = [
            asyncio.create_task(self._watch_review(), name="review-watch"),
            asyncio.create_task(self._watch_code(), name="code-watch"),
            asyncio.create_task(self._start_first_kernel(), name="first-kernel"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        with _RUNNING_LOCK:
            if _RUNNING.get(self.project.root) is self:
                del _RUNNING[self.project.root]
        for task in self._tasks:
            task.cancel()
        for page in list(self.pages):
            page.close()
        kernels = [k for k in (self.kernel, self.pending, *self.retired) if k is not None]
        await asyncio.gather(*(k.stop(grace=3.0) for k in kernels), return_exceptions=True)
        for task in list(self._background):
            task.cancel()

    def spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """A background task the hub keeps a reference to (and logs if it fails)."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None and not isinstance(t.exception(), WorkbenchError):
                log.error("background task %s failed", t.get_name(), exc_info=t.exception())

        task.add_done_callback(done)
        return task

    def call(self, coro, timeout: float):
        """Run a coroutine on the hub's loop from a worker thread (an operation's HTTP handler)."""
        if self.loop is None:
            coro.close()
            raise HubNotRunning("the hub has not started")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise WorkbenchError(f"the hub did not finish within {timeout:.0f} s") from None

    # -- kernels -------------------------------------------------------------------------------

    def _new_kernel(self) -> KernelProcess:
        self.generation += 1
        k = KernelProcess(
            self.generation,
            self.kernel_command,
            cwd=self.project.root,
            log_path=self.project.ensure_logs_dir() / f"kernel-{self.generation}.log",
        )
        k.signature = code_signature(self.project)
        k.on_exit = self._kernel_exited
        return k

    async def _start_first_kernel(self) -> None:
        k = self._new_kernel()
        self.kernel = k
        self._set_state("starting")
        try:
            await k.start()
            k.hello = await k.request("hello", timeout=HELLO_TIMEOUT)
        except WorkbenchError as exc:
            if k.alive:
                await k.stop(grace=2.0)
            self._set_state("crashed", error=_error("start", exc, k.generation, detail=k.stderr_tail()))
            self._first_kernel.set()
            return
        self.manifest = k.hello.get("manifest")
        manifest_error = k.hello.get("manifest_error")
        error = None
        if manifest_error:
            error = {"kind": "manifest", "message": f"the manifest failed to import: {manifest_error.get('type')}: {manifest_error.get('message')}",
                     "traceback": manifest_error.get("traceback"), "generation": k.generation, "at": now()}
        log.info("kernel %s ready (pid %s, %s)", k.generation, k.hello.get("pid"), k.hello.get("manifest_module"))
        self._set_state("ready", error=error)
        self._first_kernel.set()
        if self.pages:
            self._maybe_auto_show()

    def _kernel_exited(self, k: KernelProcess) -> None:
        if k.stopping or self._stopping:
            return
        log.warning("%s", k.describe_exit())
        if k is self.kernel:
            self._set_state("crashed", error=_error("crash", KernelDied(k.describe_exit()), k.generation, detail=k.stderr_tail()))

    def _kernel_for(self, scene: str | None) -> KernelProcess | None:
        generation = self.scene_generation.get(scene) if scene else None
        for k in (self.kernel, *self.retired):
            if k is not None and k.generation == generation and k.alive:
                return k
        return None

    def code_changed(self) -> bool:
        return self.kernel is None or self.kernel.signature != code_signature(self.project)

    def _retire(self, old: KernelProcess) -> None:
        self.retired.append(old)

        async def later() -> None:
            deadline = time.monotonic() + self.retire_grace
            while time.monotonic() < deadline:
                if not any(self.scene_generation.get(p.scene) == old.generation for p in self.pages):
                    break
                await asyncio.sleep(0.2)
            if old in self.retired:
                self.retired.remove(old)
            await old.stop()
            log.info("kernel %s stopped", old.generation)

        self.spawn(later(), name=f"retire-{old.generation}")

    # -- state ---------------------------------------------------------------------------------

    _KEEP = object()

    def _set_state(self, state: str, error=_KEEP, activity=None) -> None:
        self.state = state
        if error is not Hub._KEEP:
            self.error = error
        self.activity = activity if state == "building" else None
        self.broadcast(self.state_message())

    def kernel_info(self) -> dict:
        k = self.kernel
        hello = (k.hello or {}) if k else {}
        return {
            "state": self.state,
            "error": self.error,
            "activity": self.activity,
            "generation": k.generation if k else None,
            "pid": hello.get("pid"),
            "process": k.pid if k else None,
            "alive": bool(k and k.alive),
            "manifest_module": hello.get("manifest_module"),
            "pending": None if self.pending is None else {"generation": self.pending.generation, "process": self.pending.pid},
            "retired": [r.generation for r in self.retired],
        }

    def scene_summary(self) -> dict | None:
        s = self.shown
        if s is None:
            return None
        return {
            "scene": s.scene,
            "assembly": s.assembly,
            "args": s.args,
            "build": s.build,
            "generation": s.generation,
            "updates": len(s.updates),
            "stale": self.stale,
            "parts": s.base.get("parts"),
            "blob": s.base["blob"],
        }

    def state_message(self) -> dict:
        return {
            "type": "wb_state",
            "project": {"name": self.project.name, "root": str(self.project.root)},
            "build": self.build_id,
            "kernel": self.kernel_info(),
            "manifest": self.manifest,
            "scene": self.scene_summary(),
            "stale": self.stale,
        }

    def full_state(self) -> dict:
        items = self.review.get("items", [])
        counts = {s: 0 for s in ("waiting_owner", "waiting_agent", "done", "dropped")}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        sel = self.selection
        return {
            **{k: v for k, v in self.state_message().items() if k != "type"},
            "scenes": self.scenes,
            "items": {
                **counts,
                "total": len(items),
                "unread_owner": sum(1 for i in items if i["unread_owner"]),
                "unread_agent": sum(1 for i in items if i["unread_agent"]),
                "unreadable": len(self.review.get("unreadable", [])),
            },
            "views": len(self.review.get("views", [])),
            "pages": len(self.pages),
            "selection": None if sel is None else {k: sel[k] for k in ("label", "kind", "assembly", "scene", "at")},
        }

    def _current_build_id(self) -> str | None:
        try:
            return build_id(self.project.root)
        except GitError:
            return None

    # -- showing -------------------------------------------------------------------------------

    def _viewer(self) -> dict:
        """The page state a build is made for: a page that shows a model keeps its camera and
        toolbar; with none, the camera is reset to fit the model."""
        showing = any(p.scene is not None for p in self.pages)
        return {"splash": not showing, "status": dict(self.page_status) if showing else {}}

    async def show(self, assembly: str, args: dict | None = None, rebuild: bool = False) -> dict:
        """Build or update `assembly` with `args` and push it to every page; the scene summary."""
        if not isinstance(assembly, str) or not assembly:
            raise InvalidInput("assembly must be a non-empty string")
        args = {} if args is None else args
        if not isinstance(args, dict):
            raise InvalidInput("args must be a JSON object")
        assert self._show_lock is not None and self._first_kernel is not None
        async with self._show_lock:
            await self._first_kernel.wait()
            t0 = time.perf_counter()
            build = await asyncio.to_thread(self._current_build_id)
            self.build_id = build
            k = self.kernel
            fresh = rebuild or k is None or not k.alive or self.code_changed()
            try:
                if fresh:
                    k, result = await self._build_fresh(assembly, args)
                else:
                    result = await self._build_here(k, assembly, args)
            except WorkbenchError as exc:
                error = WorkbenchError(f"show {assembly} {json.dumps(args)}: {exc}")
                error.http_status = exc.http_status
                raise error from exc
            return self._accept(k, result, build, fresh, t0)

    async def _build_fresh(self, assembly: str, args: dict) -> tuple[KernelProcess, dict]:
        new = self._new_kernel()
        self.pending = new
        self._set_state("building", activity={"assembly": assembly, "args": args, "generation": new.generation, "new_kernel": True, "since": now()})
        log.info("kernel %s starting to build %s %s", new.generation, assembly, json.dumps(args))
        try:
            await new.start()
            new.hello = await new.request("hello", timeout=HELLO_TIMEOUT)
            manifest_error = new.hello.get("manifest_error")
            if manifest_error:
                raise KernelError("hello", {**manifest_error, "message": f"the manifest failed to import: {manifest_error.get('message')}"})
            result = await new.request("build", {"assembly": assembly, "args": args, "viewer": self._viewer()}, timeout=BUILD_TIMEOUT)
        except WorkbenchError as exc:
            self.pending = None
            serving = self.kernel is not None and self.kernel.alive
            if isinstance(exc, KernelError) and exc.op == "build" and not serving and new.alive:
                # Nothing else serves: keep this kernel; it imported the manifest and answers.
                old, self.kernel = self.kernel, new
                self.manifest = new.hello.get("manifest")
                if old is not None and old is not new:
                    await old.stop(grace=1.0)
                serving = True
            else:
                await new.stop(grace=2.0)
            kind = "crash" if isinstance(exc, KernelDied) else "build"
            detail = new.stderr_tail() if kind == "crash" else None
            self._set_state("ready" if serving else "crashed", error=_error(kind, exc, new.generation, assembly=assembly, args=args, detail=detail))
            log.warning("kernel %s: %s", new.generation, exc)
            raise
        self.pending = None
        old = self.kernel
        self.kernel = new
        self.manifest = new.hello.get("manifest")
        if old is not None and old is not new:
            if old.alive and self.pages:
                self._retire(old)
            else:
                self.spawn(old.stop(), name=f"stop-{old.generation}")
        return new, result

    async def _build_here(self, k: KernelProcess, assembly: str, args: dict) -> dict:
        same = self.shown is not None and self.shown.assembly == assembly and self.shown.generation == k.generation
        op = "update" if same else "build"
        self._set_state("building", activity={"assembly": assembly, "args": args, "generation": k.generation, "new_kernel": False, "op": op, "since": now()})
        try:
            return await k.request(op, {"assembly": assembly, "args": args, "viewer": self._viewer()}, timeout=BUILD_TIMEOUT)
        except KernelTimeout as exc:
            k.kill()  # a builder that hangs; picks would wait behind it forever
            self._set_state("crashed", error=_error("timeout", exc, k.generation, assembly=assembly, args=args))
            raise
        except KernelDied as exc:
            self._set_state("crashed", error=_error("crash", exc, k.generation, assembly=assembly, args=args, detail=k.stderr_tail()))
            raise
        except KernelError as exc:
            self._set_state("ready", error=_error("build", exc, k.generation, assembly=assembly, args=args))
            raise

    def _accept(self, k: KernelProcess, result: dict, build: str | None, fresh: bool, t0: float) -> dict:
        incremental = bool(result.get("incremental"))
        scene = result["scene"]
        self.scene_generation[scene] = k.generation
        if incremental and self.shown is not None:
            message = {
                "type": "wb_update",
                "scene": scene,
                "previous": result["previous"],
                "assembly": result["assembly"],
                "args": result["args"],
                "build": build,
                "moved": [{"path": m["path"], "loc": m["loc"]} for m in result.get("moved", [])],  # the label is in the path
                "changed": result.get("changed", []),
                "blob": _slim_blob(result.get("blob")),
            }
            self.shown.updates.append(message)
        else:
            message = {
                "type": "wb_scene",
                "scene": scene,
                "assembly": result["assembly"],
                "args": result["args"],
                "root": result["root"],
                "build": build,
                "generation": k.generation,
                "parts": len(result["parts"]),
                "blob": _slim_blob(result["blob"]),
            }
            self.shown = Shown(base=message, generation=k.generation, signature=k.signature)
        self.stale = False
        sent = self.broadcast(message)
        summary = {
            "scene": scene,
            "assembly": result["assembly"],
            "args": result["args"],
            "build": build,
            "generation": k.generation,
            "new_kernel": fresh,
            "incremental": incremental,
            "reason": result.get("reason"),
            "parts": [p["label"] for p in result["parts"]],
            "moved": [m["label"] for m in result.get("moved", [])],
            "changed": [c["label"] for c in result.get("changed", [])],
            "blob": _slim_blob(result.get("blob")),
            "message_bytes": len(json.dumps(message, separators=(",", ":")).encode()),
            "pages": sent,
            "timings": {**result.get("timings", {}), "hub_total_s": round(time.perf_counter() - t0, 4)},
        }
        self.scenes[result["assembly"]] = {k_: summary[k_] for k_ in ("scene", "args", "build", "generation", "incremental")}
        self._set_state("ready", error=None)
        log.info(
            "showed %s %s: %s, %d bytes to %d page(s) in %.2f s",
            result["assembly"], json.dumps(result["args"]),
            f"incremental ({len(summary['moved'])} moved, {len(summary['changed'])} changed)" if incremental else "full",
            summary["message_bytes"], sent, summary["timings"]["hub_total_s"],
        )
        return summary

    def _maybe_auto_show(self) -> None:
        """A page and no scene: show the manifest's first assembly with its defaults."""
        if not self.auto_show or self.shown is not None or self._auto_showing or self.state != "ready":
            return
        assemblies = list((self.manifest or {}).get("assemblies") or {})
        if not assemblies:
            return
        self._auto_showing = True

        async def go() -> None:
            try:
                await self.show(assemblies[0], {})
            except WorkbenchError:
                pass  # reported in the kernel state
            finally:
                self._auto_showing = False

        self.spawn(go(), name="auto-show")

    def _refresh(self) -> None:
        """Rebuild what is shown in a new kernel (the page's Rebuild, or a reload after a code change)."""
        if self.shown is None:
            self._maybe_auto_show()
            return
        assembly, args = self.shown.assembly, self.shown.args

        async def go() -> None:
            try:
                await self.show(assembly, args, rebuild=True)
            except WorkbenchError:
                pass

        self.spawn(go(), name="refresh")

    # -- pages ---------------------------------------------------------------------------------

    def broadcast(self, message: dict) -> int:
        for page in list(self.pages):
            page.send(message)
        return len(self.pages)

    def page_connected(self, page: Page) -> None:
        self.pages.add(page)
        page.send({"type": "wb_hello", "settings": PAGE_SETTINGS, **{k: v for k, v in self.state_message().items() if k != "type"}})
        page.send(self.review)
        if self.selection is not None:
            page.send({"type": "wb_pick", "selection": self.selection})
        if self.shown is not None:
            self._replay(page)
            if self.state in ("ready", "crashed") and self.shown.signature != code_signature(self.project):
                self._refresh()  # a reload after a code change picks the change up
        elif self._first_kernel is not None and self._first_kernel.is_set():
            self._maybe_auto_show()

    def page_disconnected(self, page: Page) -> None:
        self.pages.discard(page)
        page.close()

    def _replay(self, page: Page) -> None:
        assert self.shown is not None
        page.send(self.shown.base)
        for update in self.shown.updates:
            page.send(update)

    async def on_page_message(self, page: Page, message: dict) -> None:
        kind = message.get("type")
        if kind == "status":
            self._on_status(page, message)
        elif kind == "shown":
            scene = message.get("scene")
            page.scene = scene if isinstance(scene, str) else None
        elif kind == "rebuild":
            self._refresh()
        elif kind == "resync":
            if self.shown is not None:
                self._replay(page)
        elif kind == "log":
            log.debug("page %s: %s", page.id, str(message.get("text"))[:500])

    def _on_status(self, page: Page, message: dict) -> None:
        status = message.get("status")
        if not isinstance(status, dict):
            return
        scene = message.get("scene") if isinstance(message.get("scene"), str) else page.scene
        self.page_status = {k: v for k, v in status.items() if k not in EVENT_KEYS}
        if "activeTool" in status:
            self.active_tool = status["activeTool"] if isinstance(status["activeTool"], str) else None
        if scene and any(k in status for k in BACKEND_KEYS):
            self.spawn(self._relay_backend(page, scene, status), name="backend")
        pick_id, via = None, None
        ids = status.get("selectedShapeIDs")
        if isinstance(ids, list):
            strings = [i for i in ids if isinstance(i, str)]
            if strings:
                pick_id, via = strings[-1], "measure tool"
        last = status.get("lastPick")
        if isinstance(last, dict) and isinstance(last.get("path"), str) and isinstance(last.get("name"), str):
            pick_id, via = f"{last['path']}/{last['name']}", "double-click"
        if pick_id and scene:
            extra = message.get("pick") if isinstance(message.get("pick"), dict) else {}
            self._pick_seq += 1
            self._pick_task = self.spawn(self._resolve_pick(page, scene, pick_id, via, extra, self._pick_seq), name="pick")

    async def _relay_backend(self, page: Page, scene: str, status: dict) -> None:
        msg = {k: status[k] for k in BACKEND_KEYS if k in status}
        msg.setdefault("activeTool", self.active_tool or "None")  # a new kernel's backend has not seen the tool yet
        k = self._kernel_for(scene)
        if k is None:
            page.send({"type": "wb_notice", "level": "error", "message": "measuring: the kernel that holds this scene is not running"})
            return
        try:
            result = await k.request("backend", {"message": msg, "scene": scene}, timeout=PICK_TIMEOUT)
        except WorkbenchError as exc:
            page.send({"type": "wb_notice", "level": "error", "message": f"measure: {exc}"})
            return
        if result.get("reply") is not None:
            page.send({"type": "wb_backend", "reply": result["reply"]})

    async def _resolve_pick(self, page: Page, scene: str, pick_id: str, via: str, extra: dict, seq: int) -> None:
        k = self._kernel_for(scene)
        if k is None:
            page.send({"type": "wb_notice", "level": "error", "message": "the kernel that holds this scene is not running; pick again once the scene is rebuilt"})
            return
        try:
            result = await k.request("resolve", {"scene": scene, "ids": [pick_id]}, timeout=PICK_TIMEOUT)
        except WorkbenchError as exc:
            page.send({"type": "wb_notice", "level": "error", "message": f"pick: {exc}"})
            return
        r = result["results"][0]
        if not r.get("found"):
            page.send({"type": "wb_notice", "level": "error", "message": f"pick not found: {r.get('error')}"})
            return
        if seq != self._pick_seq:
            return  # a newer pick arrived meanwhile
        shown = self.shown
        point, normal = extra.get("point"), extra.get("normal")
        if r["kind"] == "vertex":
            point, normal = r["world"]["point"], None
        self.selection = {
            "seq": seq,
            "assembly": shown.assembly if shown else None,
            "args": shown.args if shown else None,
            "scene": scene,
            "build": shown.build if shown else None,
            "label": r["part"]["label"],
            "kind": r["kind"],
            "geom_type": r["world"].get("geom_type"),
            "world": r["world"],
            "local": r["local"],
            "point": point if _vector(point) else None,
            "normal": normal if _vector(normal) else None,
            "camera": extra.get("camera") if isinstance(extra.get("camera"), dict) else None,
            "via": via,
            "at": now(),
        }
        self.broadcast({"type": "wb_pick", "selection": self.selection})

    async def get_selection(self) -> dict:
        task = self._pick_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), PICK_TIMEOUT)
            except TimeoutError:
                pass
        if self.selection is None:
            raise NotFound(
                "nothing is picked in the page: activate a measure tool and click a face, edge or vertex, "
                "or double-click a part"
            )
        out = dict(self.selection)
        out["current"] = self.shown is not None and out["scene"] == self.shown.scene
        return out

    # -- comments from the page ----------------------------------------------------------------

    def comment_from_page(self, data: dict) -> dict:
        """The Comment tab: an owner's comment on the current pick, with the frozen evidence (world
        point, camera, build id, the pick's resolved properties) and the page's thumbnail attached.
        Runs in a worker thread."""
        if not isinstance(data, dict):
            raise InvalidInput("expected a JSON object")
        sel = self.selection
        if sel is None:
            raise Conflict("nothing is picked; pick a part first")
        if data.get("seq") != sel["seq"]:
            raise Conflict("the pick changed since the form was filled; check the target and send again")
        title = data.get("title")
        body = data.get("body", "")
        camera = data.get("camera")
        if not isinstance(title, str) or not isinstance(body, str):
            raise InvalidInput("title and body must be strings")
        check_camera(camera, "camera")
        png = _png_from_data_url(data.get("image")) if data.get("image") is not None else None

        from workbench.ops import default_registry  # here: the ops import this module's package

        targets = [{"kind": "part", "label": sel["label"], "assembly": sel["assembly"]}]
        state = default_registry().get("comment").call({"title": title, "body": body, "targets": targets}, project=self.project)
        item = state["id"]
        evidence = {
            "assembly": sel["assembly"],
            "args": sel["args"],
            "build": sel["build"],
            "part": sel["label"],
            "kind": sel["kind"],
            "geom_type": sel["geom_type"],
            "point": sel["point"],
            "normal": sel["normal"],
            "camera": camera,
            "picked_at": sel["at"],
            "properties": {"world": sel["world"], "local": sel["local"]},
        }
        store = ReviewStore(self.project)
        with tempfile.TemporaryDirectory(prefix="wb-comment-") as tmp:
            path = Path(tmp) / review_watch.EVIDENCE_FILE
            path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            state = store.attach(item, path, make_by("owner"))
            if png is not None:
                path = Path(tmp) / review_watch.THUMBNAIL_FILE
                path.write_bytes(png)
                state = store.attach(item, path, make_by("owner"))
        self.poke_review()
        return state

    # -- watchers ------------------------------------------------------------------------------

    def poke_review(self) -> None:
        """Something wrote review/: push it now rather than at the next poll. Thread-safe."""
        if self.loop is not None and self._review_poke is not None:
            self.loop.call_soon_threadsafe(self._review_poke.set)

    async def _watch_review(self) -> None:
        assert self._review_poke is not None
        seen = await asyncio.to_thread(review_watch.listing, self.project)
        while True:
            try:
                await asyncio.wait_for(self._review_poke.wait(), self.review_poll)
            except TimeoutError:
                pass
            self._review_poke.clear()
            try:
                current = await asyncio.to_thread(review_watch.listing, self.project)
                if current == seen:
                    continue
                seen = current
                self.review = await asyncio.to_thread(review_watch.snapshot, self.project)
            except WorkbenchError as exc:  # a lock held too long; try again at the next poll
                log.warning("review: %s", exc)
                continue
            self.broadcast(self.review)

    async def _watch_code(self) -> None:
        while True:
            await asyncio.sleep(self.code_poll)
            signature = await asyncio.to_thread(code_signature, self.project)
            build = await asyncio.to_thread(self._current_build_id)
            stale = self.shown is not None and self.shown.signature != signature
            if stale != self.stale or build != self.build_id:
                self.stale, self.build_id = stale, build
                self.broadcast(self.state_message())


def _vector(value) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)


def _png_from_data_url(value) -> bytes:
    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise InvalidInput("image must be a data:image/png;base64 URL")
    if len(value) > MAX_IMAGE_BYTES * 4 // 3 + len(prefix) + 4:
        raise InvalidInput(f"image is larger than {MAX_IMAGE_BYTES // 2**20} MB")
    try:
        png = base64.b64decode(value[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        raise InvalidInput("image is not valid base64") from None
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidInput("image is not a PNG")
    return png
