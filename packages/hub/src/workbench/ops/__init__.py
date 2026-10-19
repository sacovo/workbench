"""The operation registry and the built-in operations.

`default_registry()` returns the registry with every built-in operation registered; the CLI, the
hub's HTTP routes and the MCP tools are all generated from it.
"""

from workbench.ops.registry import REGISTRY, Endpoint, Op, Param, Registry, op


def default_registry() -> Registry:
    # Imported here, not at module level, so that registry.py can be imported by the operation
    # modules without a cycle.
    from workbench.ops import bom_ops, check_ops, doc_ops, doctor, hub_ops, job_ops, ledger_ops, mcp_ops, project_ops, review_ops  # noqa: F401  (registers on import)

    return REGISTRY


__all__ = ["REGISTRY", "Endpoint", "Op", "Param", "Registry", "default_registry", "op"]
