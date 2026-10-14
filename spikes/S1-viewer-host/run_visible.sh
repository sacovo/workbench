#!/usr/bin/env bash
# One command for the owner: the full robot in a visible Chromium window on the GPU, through the S1
# page and host, with the orbit frame rate measured in the page. See driver/visible.mjs for options
# (--size 2560x1440, --angle vulkan|gl-egl). Keep the window on screen while it runs (about 20 s).
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || pnpm install --frozen-lockfile
[ -f dist/index.html ] || pnpm run build
env -u VIRTUAL_ENV uv sync --quiet
exec node driver/visible.mjs "$@"
