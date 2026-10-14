"""The MCP shim: `wb mcp` runs a stdio MCP server exposing a curated set of the operation
registry's ops as tools (docs/plan.md, design principle 2: HTTP routes and MCP tools are
generated from the same operation registry, so they cannot drift).

    server.py   the tool list (from Registry.mcp_tools()), dispatch through workbench.cli.execute,
                the stdio server itself

`plugin/.mcp.json` starts it with the workbench environment's own interpreter; see
workbench.ops.mcp_ops for the `wb mcp` operation and its project-resolution rule.
"""

from workbench.mcp.server import TOOL_NAMES, build_server, build_tools, run_stdio

__all__ = ["TOOL_NAMES", "build_server", "build_tools", "run_stdio"]
