#!/usr/bin/env bash
# MCP is a protocol server: report startup errors to Codex.
set -u
home_dir=~
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if command -v speakd-mcp >/dev/null 2>&1; then
  exec speakd-mcp "$@"
fi

for base in "${SPEAKD_HOME:-}" "$root"; do
  [ -n "$base" ] || continue
  if [ -x "$base/.venv/bin/speakd-mcp" ]; then
    exec "$base/.venv/bin/speakd-mcp" "$@"
  fi
  if [ -x "$base/.venv/bin/python" ]; then
    exec "$base/.venv/bin/python" -c 'from speakd.clients.mcp.server import main; main()' "$@"
  fi
done
if [ -x "$home_dir/.local/bin/speakd-mcp" ]; then
  exec "$home_dir/.local/bin/speakd-mcp" "$@"
fi
echo "speakd-mcp unavailable; set SPEAKD_HOME to the speakd checkout and run uv sync there" >&2
exit 1
