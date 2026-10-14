"""`wb serve`: the hub in the foreground, on 127.0.0.1, until SIGINT or SIGTERM.

The socket is bound before anything else, so the port (ephemeral by default) is known when
`.workbench/hub.json` is written with the pid, the port and a fresh random token. The file is
removed on the way out if it still names this process. Kernels are stopped with the hub; a kernel
whose hub is killed outright ends on its own when its stdin closes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import sys

import uvicorn

from workbench.errors import Conflict, WorkbenchError
from workbench.hub.app import create_app
from workbench.hub.hub import Hub
from workbench.hub.launch import open_window
from workbench.project import HubInfo, Project, clear_hub_info, read_hub_info, running_hub, write_hub_info

log = logging.getLogger("workbench.hub")


def _logging() -> None:
    logger = logging.getLogger("workbench")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s wb serve: %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        raise WorkbenchError(f"cannot listen on 127.0.0.1:{port}: {exc.strerror or exc}") from None
    sock.set_inheritable(False)
    return sock


def run(project: Project, port: int = 0, open_window_too: bool = False, kernel_command: list[str] | None = None) -> dict:
    running, _ = running_hub(project)
    if running is not None:
        raise Conflict(f"a hub already runs for {project.name}: pid {running.pid}, {running.url}")
    _logging()
    sock = bind(port)
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    hub = Hub(project, token, port, kernel_command=kernel_command)
    info = HubInfo(pid=os.getpid(), port=port, token=token)
    server: uvicorn.Server | None = None

    async def started() -> None:
        log.info("%s on %s (pid %s); `wb open` shows the page", project.name, info.url, info.pid)
        if open_window_too:

            async def when_listening() -> None:
                while server is not None and not server.started:
                    await asyncio.sleep(0.05)
                try:
                    opened = await asyncio.to_thread(open_window, project, info)
                    log.info("opened the page (chromium pid %s)", opened["pid"])
                except WorkbenchError as exc:
                    log.warning("could not open the page: %s", exc)

            hub.spawn(when_listening(), name="open-window")

    async def stopped() -> None:
        _forget(project, info)
        log.info("stopped")

    app = create_app(hub, on_started=started, on_stopped=stopped)
    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
        lifespan="on",
        ws_max_size=64 * 2**20,
        ws_ping_interval=None,  # the page is local; a busy tab must not be disconnected
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    write_hub_info(project, port=port, token=token)
    try:
        # On SIGTERM uvicorn shuts down gracefully (lifespan included) and then re-raises the signal
        # with the default handler, which ends the process; on SIGINT that is a KeyboardInterrupt.
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        _forget(project, info)
        sock.close()
    return {"stopped": True, "url": f"{info.url}/", "port": port, "pid": info.pid}


def _forget(project: Project, info: HubInfo) -> None:
    """Remove .workbench/hub.json if it still names this hub."""
    try:
        current = read_hub_info(project)
    except WorkbenchError:
        return
    if current is not None and current.pid == info.pid and current.token == info.token:
        clear_hub_info(project)
