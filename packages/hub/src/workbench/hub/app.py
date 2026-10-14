"""The hub's HTTP and WebSocket surface (Starlette), bound to 127.0.0.1 by the server.

Every request passes the guard first:
- Host must be the hub's own address (127.0.0.1:<port> or localhost:<port>), against DNS rebinding;
- a request that carries an Origin header must come from the hub's own origin
  (http://127.0.0.1:<port>, or localhost), so another web page in the owner's browser cannot call in;
- `/api/...` needs the token from `.workbench/hub.json` in the header `X-Workbench-Token`, and the
  page's WebSocket `/ws` in its query (`/ws?token=...`, browsers cannot set headers there).
The page itself and its assets carry no secrets and need no token; the page reads the token from
the URL fragment `wbench open` puts it in.

A second gate sits in front of the registry endpoints and `/api/page/comment`: a call that writes
as the owner (Op.writes_as_owner) must also carry the page credential in `X-Workbench-Page-
Credential`, matching the one `Hub` keeps in memory (never in hub.json, a log or an op response).
The guard's token alone is not enough for these, because an agent can read that token from
hub.json too; the page credential reaches only a window the hub itself opened (workbench.hub.
launch, the `open` operation below). This is a guard against mistakes (the owner's own CLI, run
without CLAUDECODE, still answers as the owner with just the token), not a security boundary
against the same Unix user.

Routes:
    POST /api/op/<name>            every registry operation except serve and mcp (they start a
                                   hub or an MCP server, not something a hub does); a call that
                                   writes as the owner also needs the page credential
    GET  /api/state                project, build id, kernel state, scenes, item counts
    GET  /api/blob/<sha>           a data message the kernel wrote to .workbench/blobs/
    GET  /api/asset/<item>/<name>  a file attached to a review item (thumbnails)
    POST /api/page/comment         the Comment tab: a comment on the pick with frozen evidence
                                   (always an owner write; always needs the page credential)
    WS   /ws                       the page
    GET  /, /assets/...            the page, built from ui/ into static/
`extra_routes` are mounted before the page, under the same guard (the agent link adds its own).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from workbench.errors import WorkbenchError
from workbench.hub.hub import Hub, Page
from workbench.ops import Endpoint, default_registry
from workbench.review.schema import ITEM_ID_RE
from workbench.review.store import ASSET_NAME_RE

log = logging.getLogger("workbench.hub")

STATIC = Path(__file__).resolve().parent / "static"
TOKEN_HEADER = "x-workbench-token"
PAGE_HEADER = "x-workbench-page-credential"
NOT_OVER_HTTP = frozenset({"serve", "mcp"})  # they start their own process, not something a hub does
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BODY = 32 * 2**20  # bytes: a comment's thumbnail is the largest body
OWNER_WRITE_REFUSED = (
    "owner actions need the page credential; open the page with `wbench open` or `serve --open`"
)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _page_credential_ok(request: Request, hub: Hub) -> bool:
    given = request.headers.get(PAGE_HEADER) or ""
    return hmac.compare_digest(given.encode(), hub.page_credential.encode())


class Guard:
    """ASGI middleware: Host, Origin and token checks before anything else runs."""

    def __init__(self, app, hub: Hub):
        self.app = app
        self.hub = hub

    @property
    def hosts(self) -> tuple[str, str]:
        return (f"127.0.0.1:{self.hub.port}", f"localhost:{self.hub.port}")

    def problem(self, scope) -> tuple[int, str] | None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host")
        if host not in self.hosts:
            return 403, f"Host {host!r} is not this hub"
        origin = headers.get("origin")
        if origin is not None and origin not in tuple(f"http://{h}" for h in self.hosts):
            return 403, f"requests from {origin} are refused; only the hub's own page (http://127.0.0.1:{self.hub.port}) may call it"
        path = scope.get("path", "")
        if scope["type"] == "websocket":
            token = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token") or [None])[0]
        elif path.startswith("/api/"):
            token = headers.get(TOKEN_HEADER)
        else:
            return None
        if not token or not hmac.compare_digest(token.encode(), self.hub.token.encode()):
            return 401, "missing or wrong token (X-Workbench-Token, from .workbench/hub.json)"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        problem = self.problem(scope)
        if problem is None:
            return await self.app(scope, receive, send)
        status, message = problem
        if scope["type"] == "http":
            return await _error(status, message)(scope, receive, send)
        # A WebSocket refused before accept: the server answers the handshake with HTTP 403.
        await receive()
        await send({"type": "websocket.close", "code": 1008, "reason": message[:120]})


async def _json_body(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise WorkbenchError(f"request body larger than {MAX_BODY // 2**20} MB")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise WorkbenchError(f"the body is not valid JSON: {exc}") from None


def _op_route(endpoint: Endpoint, hub: Hub) -> Route:
    o = default_registry().get(endpoint.name)  # same Op the endpoint wraps; for writes_as_owner

    async def handle(request: Request) -> Response:
        try:
            body = await _json_body(request)
            if o.writes_as_owner(body) and not _page_credential_ok(request, hub):
                return _error(403, OWNER_WRITE_REFUSED)
            result = await run_in_threadpool(endpoint.call, body)
        except WorkbenchError as exc:
            return _error(exc.http_status, str(exc))
        except Exception as exc:  # noqa: BLE001 - a bug; the caller gets a message, the log the traceback
            log.exception("operation %s failed", endpoint.name)
            return _error(500, f"internal error in {endpoint.name}: {type(exc).__name__}: {exc}")
        finally:
            hub.poke_review()  # most operations write review/; the page should not wait for the poll
        return JSONResponse(result)

    return Route(endpoint.path, handle, methods=[endpoint.method], name=f"op-{endpoint.name}")


def create_app(
    hub: Hub, *, extra_routes: Sequence[BaseRoute] = (), static_dir: Path = STATIC, on_started=None, on_stopped=None
) -> Guard:
    """The hub's ASGI app for `hub`. `extra_routes` are mounted before the page's routes;
    `on_started` and `on_stopped` are awaited after the hub starts and after it stops."""

    async def state(request: Request) -> Response:
        return JSONResponse(hub.full_state())

    async def blob(request: Request) -> Response:
        sha = request.path_params["sha"]
        if not SHA_RE.match(sha):
            return _error(400, "a blob is named by its sha256 in hex")
        path = hub.project.blobs_dir / f"{sha}.json"
        if not path.is_file():
            return _error(404, f"no blob {sha}")
        return FileResponse(path, media_type="application/json", headers={"Cache-Control": "private, max-age=31536000, immutable"})

    async def asset(request: Request) -> Response:
        item, name = request.path_params["item"], request.path_params["name"]
        if not ITEM_ID_RE.match(item) or not ASSET_NAME_RE.match(name):
            return _error(400, "expected /api/asset/R-0001/<file name>")
        path = hub.project.root / "review" / "assets" / item / name
        if not path.is_file():
            return _error(404, f"no attachment review/assets/{item}/{name}")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def comment(request: Request) -> Response:
        if not _page_credential_ok(request, hub):
            return _error(403, OWNER_WRITE_REFUSED)
        try:
            body = await _json_body(request)
            result = await run_in_threadpool(hub.comment_from_page, body)
        except WorkbenchError as exc:
            log.warning("comment from the page refused: %s", exc)
            return _error(exc.http_status, str(exc))
        return JSONResponse(result)

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        page = Page(websocket)
        writer = asyncio.create_task(page.writer(), name=f"page-{page.id}-writer")
        hub.page_connected(page)
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await hub.on_page_message(page, message)
        except WebSocketDisconnect:
            pass
        finally:
            hub.page_disconnected(page)
            writer.cancel()

    async def index(request: Request) -> Response:
        page = static_dir / "index.html"
        if not page.is_file():
            return PlainTextResponse(f"The page is not built: run `pnpm install && pnpm build` in ui/ (expected {page}).\n", status_code=503)
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @asynccontextmanager
    async def lifespan(app):
        await hub.start()
        if on_started is not None:
            await on_started()
        try:
            yield
        finally:
            await hub.stop()
            if on_stopped is not None:
                await on_stopped()

    endpoints = [e for e in default_registry().http_endpoints(hub.project) if e.name not in NOT_OVER_HTTP]
    routes: list[BaseRoute] = [
        Route("/api/state", state, methods=["GET"]),
        Route("/api/blob/{sha}", blob, methods=["GET"]),
        Route("/api/asset/{item}/{name}", asset, methods=["GET"]),
        Route("/api/page/comment", comment, methods=["POST"]),
        *(_op_route(e, hub) for e in endpoints),
        WebSocketRoute("/ws", ws),
        *extra_routes,
        Route("/", index, methods=["GET"]),
        Route("/index.html", index, methods=["GET"]),
    ]
    if (static_dir / "assets").is_dir():
        routes.append(Mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets"))

    async def not_found(request: Request, exc) -> Response:
        return _error(404, f"no route {request.method} {request.url.path}")

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={404: not_found})
    return Guard(app, hub)


__all__ = ["STATIC", "Guard", "create_app"]
