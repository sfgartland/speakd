#!/usr/bin/env bash
# Find speakd-claude-hook and run it. Never fail: a non-zero exit here
# reaches Claude Code as a hook failure, and the user's turn is worth more
# than any diagnostic we could return.
set -u
# Not `set -e`: the `|| true` below is what keeps a failing entry point from
# becoming a failing hook if one is ever added. Today the `exit 0` after each
# call does that on its own, so the guards look redundant -- they are the
# thing that stays correct when the next person tightens the shell options.

if command -v speakd-claude-hook >/dev/null 2>&1; then
  speakd-claude-hook || true
  exit 0
fi

# The plugin usually sits inside the speakd checkout; fall back to the venv.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
for candidate in "$root/.venv/bin/speakd-claude-hook" "$HOME/.local/bin/speakd-claude-hook"; do
  if [ -x "$candidate" ]; then
    "$candidate" || true
    exit 0
  fi
done

exit 0
