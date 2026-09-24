#!/usr/bin/env bash
# Find speakd-mcp and run it in this process's place, so Claude Code talks to
# the server directly over stdin and stdout.
#
# The same lookup hooks/speakd-hook.sh makes, in the same order: on PATH, then
# $SPEAKD_HOME's venv, then the checkout this file sits in, then ~/.local/bin.
# An installed copy of the plugin cannot point back at the checkout by itself,
# so $SPEAKD_HOME is what carries it across -- the README says to set it.
#
# Unlike the hook wrapper this may fail loudly: an MCP server that cannot
# start is reported by Claude Code as one, which is the right place for it,
# and nothing here sits in the user's turn.
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
