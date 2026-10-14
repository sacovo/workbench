"""`python -m workbench ...` runs the same CLI as the `wb` console script.

For plugin/.mcp.json, which starts the MCP shim with the workbench environment's own interpreter
directly (no console script on its PATH): `.../.venv/bin/python -m workbench mcp`.
"""

import sys

from workbench.cli import main

if __name__ == "__main__":
    sys.exit(main())
