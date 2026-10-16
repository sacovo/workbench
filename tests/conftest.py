"""Fixtures shared by test modules: `project`, a throwaway git project (hub_helpers), and `herdr`,
a fake herdr and claude on PATH (test_agentlink_transport)."""

from hub_helpers import project  # noqa: F401  (registers the fixture for every module)
from test_agentlink_transport import herdr  # noqa: F401  (likewise)
