#!/usr/bin/env bash
# Codex hooks must never interrupt a turn. Preserve stdout for prompt context.
set -u
home_dir=~
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [ -n "${SPEAKD_STATE_DIR:-}" ]; then
  log_dir="$SPEAKD_STATE_DIR/codex"
else
  log_dir="${XDG_STATE_HOME:-$home_dir/.local/state}/speakd/codex"
fi

log() {
  { mkdir -p "$log_dir"; } 2>/dev/null || return 0
  file="$log_dir/hook.log"
  size=0
  if [ -f "$file" ]; then
    size=$( { wc -c <"$file"; } 2>/dev/null ) || size=0
  fi
  if [ "${size:-0}" -gt 262144 ] 2>/dev/null; then
    { : >"$file"; } 2>/dev/null || true
  fi
  { printf '%(%Y-%m-%dT%H:%M:%S)T %s\n' -1 "$1" >>"$file"; } 2>/dev/null || true
}

run_entry_point() {
  exec 3>&1
  complaint="$("$@" 2>&1 1>&3)"
  status=$?
  exec 3>&-
  complaint="${complaint//$'\n'/ }"
  complaint="${complaint:0:400}"
  if [ -n "$complaint" ] || [ "$status" -ne 0 ]; then
    log "$1 exited $status: ${complaint:-no message}"
  fi
  exit 0
}

if command -v speakd-codex-hook >/dev/null 2>&1; then
  run_entry_point speakd-codex-hook
fi
for base in "${SPEAKD_HOME:-}" "$root"; do
  [ -n "$base" ] || continue
  if [ -x "$base/.venv/bin/speakd-codex-hook" ]; then
    run_entry_point "$base/.venv/bin/speakd-codex-hook"
  fi
  if [ -x "$base/.venv/bin/python" ]; then
    run_entry_point "$base/.venv/bin/python" -c 'from speakd.clients.codex.hook import main; main()'
  fi
done
if [ -x "$home_dir/.local/bin/speakd-codex-hook" ]; then
  run_entry_point "$home_dir/.local/bin/speakd-codex-hook"
fi
log "could not find speakd-codex-hook; set SPEAKD_HOME to the speakd checkout and run uv sync there"
exit 0
