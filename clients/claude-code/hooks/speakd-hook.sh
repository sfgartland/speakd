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

# Matches hook.LOG_CAP_BYTES. Both halves write to one file, so both have to
# bound it, or the half that does not is the one that fills the disk.
LOG_CAP_BYTES=262144

log() {
  directory="$(state_dir)"
  mkdir -p "$directory" 2>/dev/null || return 0
  file="$directory/hook.log"
  # `wc -c` rather than stat(1): stat's flags differ between GNU and BSD, and
  # this runs on whatever the user has. The redirect is inside a group whose
  # stderr is discarded -- a bare `<"$file" 2>/dev/null` still reports a
  # missing file, because the shell opens the input before the command's own
  # redirect applies, and that lands on Claude Code's transcript.
  size=0
  if [ -f "$file" ]; then
    size=$( { wc -c <"$file"; } 2>/dev/null ) || size=0
  fi
  if [ "${size:-0}" -gt "$LOG_CAP_BYTES" ] 2>/dev/null; then
    { : >"$file"; } 2>/dev/null || true
  fi
  # printf's %(...)T rather than date(1): with PATH unset there may be no
  # date to call, and a diagnostic that needs its own diagnostic is no use.
  # Grouped like the two above: a root-owned log left by one `sudo claude`,
  # or a state directory on a mount that went read-only, otherwise reports
  # "Permission denied" into the transcript on every hook from then on.
  { printf '%(%Y-%m-%dT%H:%M:%S)T %s\n' -1 "$1" >>"$file"; } 2>/dev/null || true
}

# Run the entry point, then leave. Its stderr is captured rather than let
# through: an executable that `[ -x ]` accepts can still fail to run -- a venv
# rebuilt on a Python that has gone, or a checkout moved after `uv sync` wrote
# absolute shebangs into it -- and bash reports "bad interpreter" on stderr,
# which Claude Code shows in the transcript while the log the README sends the
# user to stays empty. stdout is passed through untouched on fd 3; the entry
# point is tested never to write there, and swallowing it here would hide it
# if that ever changed.
run_entry_point() {
  complaint="$( { "$@" 2>&1 1>&3 3>&-; } 3>&1 )"
  status=$?
  complaint="${complaint//$'\n'/ }"
  complaint="${complaint:0:400}"
  if [ "$status" -eq 126 ] || [ "$status" -eq 127 ]; then
    log "could not run $1 (exit $status): ${complaint:-no message} -- the venv \
may have been built against a Python that is no longer installed, or the \
checkout has moved; recreate it with \`uv sync\` in your speakd checkout"
  elif [ -n "$complaint" ]; then
    log "$1 wrote to stderr: $complaint"
  elif [ "$status" -ne 0 ]; then
    log "$1 exited $status"
  fi
  exit 0
}

if command -v speakd-claude-hook >/dev/null 2>&1; then
  run_entry_point speakd-claude-hook
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
    run_entry_point "$candidate"
  fi
done

# Still 0 -- but never silent. Someone who installed the plugin, started the
# daemon and heard nothing is told by the README to tail this file, and an
# empty file would send them looking at the daemon instead of at this.
log "could not find speakd-claude-hook: not on PATH, and no executable at \
\${SPEAKD_HOME}/.venv/bin (SPEAKD_HOME=${SPEAKD_HOME:-unset}), $root/.venv/bin, \
or \${HOME}/.local/bin -- set SPEAKD_HOME to your speakd checkout"
exit 0
