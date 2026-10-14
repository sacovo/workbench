"""Forward an operation to the running hub (for operations registered with needs_hub=True).

The hub's address and token come from `.workbench/hub.json`. Proxies are bypassed: the hub only
listens on 127.0.0.1, and an `http_proxy` in the environment must not see the token.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from workbench.errors import Busy, Conflict, HubNotRunning, InvalidInput, NotFound, WorkbenchError
from workbench.project import Project, running_hub

TOKEN_HEADER = "X-Workbench-Token"
FORWARD_TIMEOUT = 120.0  # s; operations that need the hub may build geometry
NOT_RUNNING = "hub not running: start it with `wbench serve`"

_BY_STATUS = {400: InvalidInput, 404: NotFound, 409: Conflict, 503: Busy}


def forward(project: Project, name: str, arguments: dict, timeout: float = FORWARD_TIMEOUT) -> dict:
    hub, why = running_hub(project)
    if hub is None:
        raise HubNotRunning(f"{NOT_RUNNING} ({why})")
    request = urllib.request.Request(
        f"{hub.url}/api/op/{name}",
        data=json.dumps(arguments).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", TOKEN_HEADER: hub.token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            message = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            message = body.decode("utf-8", errors="replace").strip() or exc.reason
        error = _BY_STATUS.get(exc.code, WorkbenchError)(f"hub: {message}")
        error.http_status = exc.code
        raise error from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise HubNotRunning(f"{NOT_RUNNING} (connection refused on port {hub.port})") from None
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise WorkbenchError(f"hub did not answer {name} within {timeout:.0f} s") from None
        raise WorkbenchError(f"hub unreachable: {exc.reason}") from None
    except (socket.timeout, TimeoutError):
        raise WorkbenchError(f"hub did not answer {name} within {timeout:.0f} s") from None
    try:
        result = json.loads(payload)
    except ValueError:
        raise WorkbenchError(f"hub answered {name} with something that is not JSON") from None
    if not isinstance(result, dict):
        raise WorkbenchError(f"hub answered {name} with {type(result).__name__}, not an object")
    return result
