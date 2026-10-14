"""The MCP shim operation: `wb mcp` runs a stdio server (workbench.mcp), for plugin/.mcp.json.

Not forwarded, not an HTTP endpoint (workbench.hub.app.NOT_OVER_HTTP) and never a tool of its own
server: like `serve`, it runs a process, not a single request. workbench.mcp is imported inside
the function so that every other `wb` command stays free of the mcp package's import time.
"""

from __future__ import annotations

import os
import sys

from workbench.ops.registry import op
from workbench.project import find_project


@op("mcp", summary="Run a stdio MCP server exposing the agent's wb commands as tools")
def mcp() -> dict:
    """Lists tools from the operation registry's own schemas (status, inbox, item, reply, ask,
    note, view-create, view-list, view-get, show, selection, agents); each call runs the same
    path `wb` does, a local operation here or a hub one forwarded to the running hub. The project
    is found from the working directory, or from CLAUDE_PROJECT_DIR when it is set (plugin/.mcp.json
    passes it; this command is meant to be started once by the plugin, not aimed at a project by
    hand, so `wb`'s own `-C` is not read here).

    Exits the process as soon as the client disconnects, instead of returning: stdout is the
    JSON-RPC wire for as long as the server runs, and the CLI's own trailing print of this
    operation's result would otherwise land on that same pipe.
    """
    from workbench.mcp import run_stdio

    project = find_project(os.environ.get("CLAUDE_PROJECT_DIR"))
    run_stdio(str(project.root))
    sys.exit(0)
