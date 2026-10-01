#!/usr/bin/env bash
# Put speakd in the app menu. The entry runs packaging/desktop/speakd-gui,
# which runs the newest build of the shell in this checkout, and the shell runs
# the daemon -- so nothing is copied out of the checkout to fall behind it.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
entry="$app_dir/speakd.desktop"

if ! compgen -G "$root/clients/gui/app/src-tauri/target/*/speakd-shell" >/dev/null; then
  printf 'install-gui: warning: the shell is not built yet\n' >&2
  printf '    cd %s/clients/gui/app/src-tauri && cargo build --release\n' "$root" >&2
fi

mkdir -p "$app_dir"
# The window's app id, so the dock files it under this entry: tao takes it
# from the binary's name.
sed -e "s|@ROOT@|$root|g" -e "s|@WMCLASS@|speakd-shell|g" \
  "$root/packaging/desktop/speakd.desktop.in" > "$entry"
command -v update-desktop-database >/dev/null && update-desktop-database "$app_dir" || true

printf 'Wrote %s\n\n' "$entry"
printf 'speakd is in the app menu. Opening it starts the daemon too; the tray'"'"'s\n'
printf 'Quit stops both, and if either one dies the other goes with it.\n'
if systemctl --user is-enabled --quiet speakd.service 2>/dev/null ||
   systemctl --user is-active --quiet speakd.service 2>/dev/null; then
  printf '\nThe speakd systemd service is set up as well. The app runs its own daemon,\n'
  printf 'and two cannot share the socket, so retire the service:\n'
  printf '    systemctl --user disable --now speakd\n'
fi
