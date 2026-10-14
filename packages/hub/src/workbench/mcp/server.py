"""A stdio MCP server exposing a curated subset of the operation registry as tools.

Tool definitions (name, description, input schema) come from Registry.mcp_tools(), the same
schemas the hub's HTTP routes use (workbench.ops.registry, docs/plan.md design principle 2): never
hand-written here, so a tool cannot drift from its operation. Only the agent's own interaction ops
are exposed (TOOL_NAMES), not the owner-only ones (answer, comment: the UI calls them directly,
never the agent) or the ops that run a process instead of answering one request (serve, open, mcp
itself).

Each call runs through workbench.cli.execute(), off the event loop: a local operation runs in this
process, one that needs_hub is forwarded over HTTP to the running hub, exactly as the CLI does.
Anything that goes wrong (bad arguments, no hub, a hub error) comes back as a tool error
(CallToolResult.is_error with the message as its content), not a protocol-level one, so the agent
sees the real message and can self-correct; left to the mcp package's own default, an exception
raised from the handler is masked as a generic "Internal server error"
(mcp.server.runner.modern_error_data), which would hide every WorkbenchError's message.
"""

from __future__ import annotations

import json
import logging

import anyio
import anyio.to_thread
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from workbench import __version__
from workbench.cli import execute
from workbench.errors import WorkbenchError
from workbench.ops import Registry, default_registry

log = logging.getLogger("workbench.mcp")

# The registry's interaction ops: everything an agent does through the workbench skill
# (plugin/skills/workbench/SKILL.md) except answer and comment, which are owner_only, and serve,
# open and mcp itself, which run a process rather than answer one request. _NEVER checks that
# again below, in case the registry ever adds one of these names to the list by mistake.
TOOL_NAMES = (
    "status",
    "inbox",
    "item",
    "reply",
    "ask",
    "note",
    "view-create",
    "view-list",
    "view-get",
    "show",
    "selection",
    "agents",
)
_NEVER = frozenset({"serve", "open", "mcp"})


def build_tools(registry: Registry | None = None) -> list[types.Tool]:
    """The MCP tool list for TOOL_NAMES, from the registry's own Registry.mcp_tools()."""
    registry = registry or default_registry()
    ops_by_name = {o.name: o for o in registry}
    missing = [n for n in TOOL_NAMES if n not in ops_by_name]
    if missing:
        raise RuntimeError(f"wbench mcp: operation(s) not in the registry: {', '.join(missing)}")
    unsafe = sorted(n for n in TOOL_NAMES if n in _NEVER or ops_by_name[n].owner_only)
    if unsafe:
        raise RuntimeError(f"wbench mcp: operation(s) must never be exposed as a tool: {', '.join(unsafe)}")
    schemas = {t["name"]: t for t in registry.mcp_tools()}
    return [
        types.Tool(name=schemas[n]["name"], description=schemas[n]["description"], input_schema=schemas[n]["inputSchema"])
        for n in TOOL_NAMES
    ]


async def _call(op_name: str, arguments: dict, directory: str | None) -> types.CallToolResult:
    """One tool call, the way the CLI runs a command: workbench.cli.execute(), off the event loop
    (it blocks on file I/O or an HTTP round trip to the hub)."""
    try:
        op = default_registry().get(op_name)
        result = await anyio.to_thread.run_sync(execute, op, arguments, directory)
    except WorkbenchError as exc:
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], is_error=True)
    except Exception as exc:  # noqa: BLE001 - a bug; still a tool error, not a dropped session
        log.exception("wbench mcp: tool %s failed", op_name)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"internal error: {exc}")], is_error=True)
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])


def build_server(directory: str | None = None, registry: Registry | None = None) -> Server:
    """A Server whose tools/list and tools/call are wired to TOOL_NAMES, dispatching every call
    with `directory` as the project to find (workbench.ops.mcp_ops resolves it once, from the
    working directory or CLAUDE_PROJECT_DIR, before starting the server)."""
    tools = build_tools(registry)
    names = {t.name for t in tools}

    async def on_list_tools(ctx, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in names:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"no tool {params.name!r}")], is_error=True)
        return await _call(params.name, params.arguments or {}, directory)

    return Server("workbench", version=__version__, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve(directory: str | None = None) -> None:
    server = build_server(directory)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(directory: str | None = None) -> None:
    """Run the stdio MCP server on this process's stdin/stdout until the client disconnects."""
    anyio.run(serve, directory)
