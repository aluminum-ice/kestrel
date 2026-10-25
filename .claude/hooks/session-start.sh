#!/bin/bash
# Install the lint toolchain (see requirements-lint.txt) so `make lint`
# works in Claude Code cloud sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

python3 -m pip install --quiet --disable-pip-version-check \
  -r "$CLAUDE_PROJECT_DIR/requirements-lint.txt"
