"""Kernel processes driven through their protocol, as the hub drives them.

The tests run in a project environment that has the kernel installed (examples/minimal):
    cd examples/minimal && env -u VIRTUAL_ENV uv run pytest ../../packages/kernel/tests -q
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MINIMAL = TESTS.parents[2] / "examples" / "minimal"
NOISY = TESTS / "fixtures" / "noisy"

# Headless, as the example's own tests run.
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)


class KernelProcess:
    """`python -m workbench_kernel serve` in this environment. Every byte the kernel writes to
    stdout is kept, so a test can check that stdout held protocol lines and nothing else."""

    def __init__(self, project: Path, stderr_path: Path, pythonpath: Path | None = None):
        env = dict(os.environ)
        if pythonpath is not None:
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(pythonpath), env.get("PYTHONPATH")) if p)
        self.stderr_path = stderr_path
        self._stderr = stderr_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "workbench_kernel", "serve", "--project", str(project)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=env,
        )
        self.stdout_lines: list[bytes] = []
        self._buffer = b""
        self._id = 0

    def send_line(self, line: bytes) -> None:
        self.proc.stdin.write(line + b"\n")
        self.proc.stdin.flush()

    def read_line(self, timeout: float = 120.0) -> bytes:
        deadline = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise TimeoutError(f"no response within {timeout} s; stderr: {self.stderr()[-2000:]}")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise EOFError(f"kernel closed stdout; stderr: {self.stderr()[-2000:]}")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        self.stdout_lines.append(line)
        return line

    def request(self, op: str, timeout: float = 120.0, **args) -> dict:
        self._id += 1
        self.send_line(json.dumps({"id": self._id, "op": op, "args": args}).encode())
        response = json.loads(self.read_line(timeout))
        assert response["id"] == self._id, response
        return response

    def ok(self, op: str, **args) -> dict:
        response = self.request(op, **args)
        assert response["ok"], response["error"]["traceback"]
        return response["result"]

    def stderr(self) -> str:
        return self.stderr_path.read_text(errors="replace")  # written by the kernel only

    def close(self) -> bytes:
        """End stdin, wait for the kernel to exit, and return whatever else it wrote to stdout."""
        self.proc.stdin.close()
        rest = self._buffer + self.proc.stdout.read()
        self.proc.wait(timeout=60)
        self._stderr.close()
        return rest


def leaves(tree: dict) -> list[dict]:
    """The leaf nodes of a viewer tree, in order."""
    out = []
    for node in tree.get("parts") or []:
        out += leaves(node) if node.get("parts") is not None else [node]
    return out
