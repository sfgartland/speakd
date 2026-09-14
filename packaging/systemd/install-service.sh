#!/usr/bin/env bash
# Install the speakd user service, with this checkout's paths resolved in.
#
# Writes the unit and stops. Enabling a service that starts at every login is
# the user's decision, so this prints the command rather than running it.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
venv="${SPEAKD_VENV:-$root/.venv}"
template="$root/packaging/systemd/speakd.service.in"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/speakd.service"

die() { printf 'install-service: %s\n' "$1" >&2; exit 1; }

[ -f "$template" ] || die "no template at $template"

# Check the interpreter before writing anything: a unit pointing at a binary
# that does not exist fails at login with a message nobody reads.
if [ ! -x "$venv/bin/speakd" ]; then
  die "no speakd at $venv/bin/speakd
  Build the environment first:  cd $root && uv sync --extra kokoro
  Or point at another venv:     SPEAKD_VENV=/path/to/.venv $0"
fi

# Warn, but do not refuse: the daemon starts and serves without the engine,
# and says so clearly on the first enqueue.
if ! "$venv/bin/python" -c 'import kokoro' 2>/dev/null; then
  printf 'install-service: warning: kokoro is not installed in %s\n' "$venv" >&2
  printf '                 the daemon will start but cannot synthesise speech\n' >&2
fi

mkdir -p "$unit_dir"
sed -e "s|@ROOT@|$root|g" -e "s|@VENV@|$venv|g" "$template" > "$unit"

printf 'Wrote %s\n\n' "$unit"
printf 'Start it, now and at every login:\n'
printf '    systemctl --user daemon-reload\n'
printf '    systemctl --user enable --now speakd\n\n'
printf 'Then:\n'
printf '    systemctl --user status speakd      # is it up\n'
printf '    journalctl --user -u speakd -f      # what it is saying\n\n'
printf 'It takes about half a minute to load the model. The socket appearing at\n'
printf '%s/speakd/speakd.sock is the readiness signal.\n\n' "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
printf 'For the Claude Code plugin, add this to your shell profile so the copied\n'
printf 'plugin can find this checkout:\n'
printf '    export SPEAKD_HOME=%s\n' "$root"
