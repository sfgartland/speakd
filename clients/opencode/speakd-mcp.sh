#!/usr/bin/env bash
# Find speakd-mcp and run it in this process's place, so OpenCode talks to
# the server directly over stdin and stdout.
#
# The installer writes this file's absolute path into opencode.json, so an
# installed config points back at the checkout through it -- and
# $SPEAKD_HOME carries it across a moved checkout, as with Claude Code.
#
# Unlike a hook, this may fail loudly: an MCP server that cannot start is
# reported by OpenCode as one, which is the right place for it.
set -u
home_dir=~

if command -v speakd-mcp >/dev/null 2>&1; then
  exec speakd-mcp "$@"
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for candidate in \
  "${SPEAKD_HOME:-}/.venv/bin/speakd-mcp" \
  "$root/.venv/bin/speakd-mcp" \
  "$home_dir/.local/bin/speakd-mcp"; do
  if [ -x "$candidate" ]; then
    exec "$candidate" "$@"
  fi
done

echo "speakd-mcp: not on PATH, and no executable at \${SPEAKD_HOME}/.venv/bin \
(SPEAKD_HOME=${SPEAKD_HOME:-unset}), $root/.venv/bin, or $home_dir/.local/bin -- \
set SPEAKD_HOME to your speakd checkout and run \`uv sync\` there" >&2
exit 1
