"""The agent link's HTTP routes, for the hub's app: create_app(hub, extra_routes=routes(link)).

    POST /api/hooks/{event}   event: session-start, user-prompt-submit, post-tool-use or stop
                              body: the hook script's summary (plugin/hooks/wb_hook.py)
                              200 {"output": <object for the hook to print> | null}
                              400 malformed body, 401 token, 404 unknown event, 413 body too large
    GET  /api/agents          200 AgentLink.snapshot(): sessions, herdr state at the last look,
                              what waits for the agent, the latest wake attempts and notices

Both answer from memory on the event loop; nothing here waits on herdr or the disk. The hub's
Guard checks Host, Origin and the token for every /api/ route first; these routes check the token
again when the link has one, so they stay closed if they are ever mounted without the Guard.
"""

from __future__ import annotations

import hmac
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from workbench.agentlink.link import AgentLink
from workbench.errors import WorkbenchError

TOKEN_HEADER = "x-workbench-token"
MAX_HOOK_BODY = 64 * 1024  # bytes; the script sends a few hundred


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _token_ok(request: Request, link: AgentLink) -> bool:
    if link.token is None:
        return True
    given = request.headers.get(TOKEN_HEADER) or ""
    return hmac.compare_digest(given.encode(), link.token.encode())


def routes(link: AgentLink) -> list[Route]:
    async def hook(request: Request) -> Response:
        if not _token_ok(request, link):
            return _error(401, "missing or wrong token (X-Workbench-Token, from .workbench/hub.json)")
        raw = await request.body()
        if len(raw) > MAX_HOOK_BODY:
            return _error(413, f"hook body larger than {MAX_HOOK_BODY} bytes")
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            return _error(400, f"the hook body is not valid JSON: {exc}")
        try:
            output = link.handle_hook(request.path_params["event"], payload)
        except WorkbenchError as exc:
            return _error(exc.http_status, str(exc))
        return JSONResponse({"output": output})

    async def agents(request: Request) -> Response:
        if not _token_ok(request, link):
            return _error(401, "missing or wrong token (X-Workbench-Token, from .workbench/hub.json)")
        return JSONResponse(link.snapshot())

    return [
        Route("/api/hooks/{event}", hook, methods=["POST"], name="agentlink-hook"),
        Route("/api/agents", agents, methods=["GET"], name="agentlink-agents"),
    ]
