#!/usr/bin/env bash
# Find speakd-claude-hook and run it. Never fail: a non-zero exit here
# reaches Claude Code as a hook failure, and the user's turn is worth more
# than any diagnostic we could return.
set -u
# Not `set -e`: the `|| true` below is what keeps a failing entry point from
# becoming a failing hook if one is ever added. Today the `exit 0` after each
# call does that on its own, so the guards look redundant -- they are the
# thing that stays correct when the next person tightens the shell options.

# Mirrors watermark.state_dir(). Two implementations of one path, in bash and
# in Python, because a user tailing the file the README names has to see both
# the wrapper's failures and the entry point's.
state_dir() {
  if [ -n "${SPEAKD_STATE_DIR:-}" ]; then
    printf '%s/claude-code\n' "$SPEAKD_STATE_DIR"
  elif [ -n "${XDG_STATE_HOME:-}" ]; then
    printf '%s/speakd/claude-code\n' "$XDG_STATE_HOME"
  else
    printf '%s/.local/state/speakd/claude-code\n' "${HOME:-/tmp}"
  fi
}

log() {
  directory="$(state_dir)"
  mkdir -p "$directory" 2>/dev/null || return 0
  # printf's %(...)T rather than date(1): with PATH unset there may be no
  # date to call, and a diagnostic that needs its own diagnostic is no use.
  printf '%(%Y-%m-%dT%H:%M:%S)T %s\n' -1 "$1" >>"$directory/hook.log" 2>/dev/null || true
}

if command -v speakd-claude-hook >/dev/null 2>&1; then
  speakd-claude-hook || true
  exit 0
fi

# Claude Code COPIES an installed plugin into ~/.claude/plugins/cache/, so for
# the documented install this resolves inside the cache, not the checkout, and
# there is no .venv there. $SPEAKD_HOME is how the copy is told where speakd
# actually lives; the relative path still covers running straight from a
# checkout, which is what the settings.json install does.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
for candidate in \
  "${SPEAKD_HOME:-}/.venv/bin/speakd-claude-hook" \
  "$root/.venv/bin/speakd-claude-hook" \
  "${HOME:-}/.local/bin/speakd-claude-hook"; do
  if [ -x "$candidate" ]; then
    "$candidate" || true
    exit 0
  fi
done

# Still 0 -- but never silent. Someone who installed the plugin, started the
# daemon and heard nothing is told by the README to tail this file, and an
# empty file would send them looking at the daemon instead of at this.
log "could not find speakd-claude-hook: not on PATH, and no executable at \
\${SPEAKD_HOME}/.venv/bin (SPEAKD_HOME=${SPEAKD_HOME:-unset}), $root/.venv/bin, \
or \${HOME}/.local/bin -- set SPEAKD_HOME to your speakd checkout"
exit 0
