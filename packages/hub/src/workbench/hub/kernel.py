"""One kernel process and its JSON-lines protocol, seen from the hub.

A kernel runs in the PROJECT's environment, never in the hub's:

    uv run --project <root> python -m workbench_kernel serve --project <root>

with VIRTUAL_ENV removed from its environment (a foreign VIRTUAL_ENV would point uv at another
environment). It reads one request per line on stdin and answers one line per request on stdout,
in order (workbench_kernel.main):

    {"id": 7, "op": "build", "args": {...}}  ->  {"id": 7, "ok": true, "result": {...}}
                                                 {"id": 7, "ok": false, "error": {"type", "message", "traceback"}}

Requests are pipelined: each gets an id and a deadline, and the answer is matched by id. The
kernel's stderr goes to `.workbench/logs/kernel-<generation>.log`; its tail is quoted when the
process dies. The process runs in its own process group, so stopping it also stops whatever uv
started under it, and end of stdin (the hub going away) ends it too.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from workbench.errors import WorkbenchError

# A data message for the full robot is about 10 MB, but it travels as a blob file; protocol lines
# carry descriptions and pick results. The limit only has to be above any line the kernel writes.
LINE_LIMIT = 256 * 2**20
STDERR_TAIL = 4000  # characters of the kernel's log quoted when it dies


class KernelError(WorkbenchError):
    """The kernel answered a request with ok: false (a builder raised, a scene is unknown)."""

    http_status = 500

    def __init__(self, op: str, error: dict):
        self.op = op
        self.type = str(error.get("type", "Error"))
        self.kernel_message = str(error.get("message", ""))
        self.traceback = str(error.get("traceback", ""))
        if self.type == "RequestError":
            self.http_status = 400
        where = _last_frame(self.traceback)
        super().__init__(f"{op} failed: {self.type}: {self.kernel_message}" + (f" ({where})" if where else ""))


class KernelDied(WorkbenchError):
    """The kernel process is gone (crashed, killed, or never started)."""

    http_status = 503


class KernelTimeout(WorkbenchError):
    """No answer within the request's deadline."""

    http_status = 504


def _last_frame(tb: str) -> str:
    """'file.py:123' of the innermost frame of a Python traceback, or ''."""
    frames = [line.strip() for line in tb.splitlines() if line.strip().startswith('File "')]
    if not frames:
        return ""
    last = frames[-1]  # File "/path/x.py", line 12, in f
    try:
        path = last.split('"')[1]
        line = last.split(", line ")[1].split(",")[0]
    except IndexError:
        return ""
    return f"{path}:{line}"


def kernel_command(root: Path) -> list[str]:
    return ["uv", "run", "--project", str(root), "python", "-m", "workbench_kernel", "serve", "--project", str(root)]


# Variables a kernel must not inherit: a foreign virtual environment (uv would use it), the stale
# NVIDIA settings, and any display (a kernel never opens a window).
_DROP_ENV = ("VIRTUAL_ENV", "__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR", "DISPLAY", "WAYLAND_DISPLAY")


def kernel_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _DROP_ENV}


class KernelProcess:
    """One kernel process: start, pipelined requests with deadlines, stop."""

    def __init__(self, generation: int, command: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None):
        self.generation = generation
        self.command = list(command)
        self.cwd = cwd
        self.log_path = log_path
        self.env = kernel_env() if env is None else env
        self.proc: asyncio.subprocess.Process | None = None
        self.hello: dict | None = None
        self.signature: str | None = None  # the code state it was started on (see Hub.code_signature)
        self.started_at = time.time()
        self.returncode: int | None = None
        self.stopping = False  # set when the hub stops it on purpose
        self.exited = asyncio.Event()
        self.on_exit: Callable[[KernelProcess], None] | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        return None if self.proc is None else self.proc.pid

    @property
    def alive(self) -> bool:
        return self.proc is not None and not self.exited.is_set()

    async def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as log:
            log.write(f"--- kernel {self.generation} started {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(self.command)}\n".encode())
            log.flush()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                    start_new_session=True,
                    limit=LINE_LIMIT,
                )
            except OSError as exc:
                self.returncode = -1
                self.exited.set()
                raise KernelDied(f"kernel {self.generation} could not start: {exc}") from None
        self._reader = asyncio.create_task(self._read(), name=f"kernel-{self.generation}-reader")

    def stderr_tail(self, limit: int = STDERR_TAIL) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - limit))
                return f.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def describe_exit(self) -> str:
        code = self.returncode
        how = f"was killed by signal {-code}" if code is not None and code < 0 else f"exited with code {code}"
        if code == 137:
            how += " (killed)"  # uv run reports a child killed by SIGKILL as 128 + 9
        kernel_pid = (self.hello or {}).get("pid")
        pids = f"pid {kernel_pid}, started as pid {self.pid}" if kernel_pid not in (None, self.pid) else f"pid {self.pid}"
        return f"kernel {self.generation} ({pids}) {how}"

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # stdout carries protocol lines only; anything else is ignored
                if not isinstance(message, dict):
                    continue
                future = self._pending.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except (asyncio.LimitOverrunError, ValueError):
            pass  # a line above LINE_LIMIT: treated as the end of this kernel
        finally:
            self.returncode = await self.proc.wait()
            died = KernelDied(f"{self.describe_exit()}" + (f"; last output:\n{self.stderr_tail(1500)}" if not self.stopping else ""))
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(died)
            self._pending.clear()
            self.exited.set()
            if self.on_exit is not None:
                self.on_exit(self)

    async def request(self, op: str, args: dict | None = None, timeout: float = 60.0) -> dict:
        """Send one request and wait for its answer; KernelError if it failed, KernelDied if the
        process went away, KernelTimeout after `timeout` seconds."""
        if self.proc is None or self.exited.is_set():
            raise KernelDied(f"kernel {self.generation} is not running" + (f" ({self.describe_exit()})" if self.returncode is not None else ""))
        self._next_id += 1
        rid = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        line = json.dumps({"id": rid, "op": op, "args": args or {}}, allow_nan=False).encode() + b"\n"
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self._pending.pop(rid, None)
            await self.exited.wait()
            raise KernelDied(self.describe_exit()) from None
        try:
            message = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._pending.pop(rid, None)
            raise KernelTimeout(f"kernel {self.generation} did not answer {op} within {timeout:.0f} s") from None
        if not message.get("ok"):
            raise KernelError(op, message.get("error") or {})
        result = message.get("result")
        if not isinstance(result, dict):
            raise KernelError(op, {"type": "ProtocolError", "message": f"result of {op} is not an object"})
        return result

    async def stop(self, grace: float = 5.0) -> None:
        """End stdin (the kernel exits), then SIGTERM and SIGKILL to its process group if it lingers."""
        self.stopping = True
        if self.proc is None:
            return
        if not self.exited.is_set():
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.close()
            except (OSError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(self.exited.wait(), grace)
            except TimeoutError:
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.exited.wait(), 2.0)
                except TimeoutError:
                    self._signal(signal.SIGKILL)
                    await self.exited.wait()

    def kill(self) -> None:
        """SIGKILL the whole process group (tests, and a hub that must not wait)."""
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        # Only while the group leader has not been reaped: afterwards its id may name another group.
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
