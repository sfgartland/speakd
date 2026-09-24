#!/usr/bin/env bash
# Start (or restart) the headless test Zotero on a profile made by setup-profile.sh.
# Stops only that profile's instance: the pattern is that profile's path,
# ending where Zotero's command line ends it, and its first character is
# bracketed so that pkill does not match this script's own command line.
set -u
DIR="$(realpath -m "${1:-$HOME/.cache/speakd-zotero-test}")"
PORT="$(cat "$DIR/port" 2>/dev/null || echo 23129)"
export ZOTERO_TEST_DIR="$DIR"
PATTERN="-profile [${DIR:0:1}]${DIR:1}/profile( |\$)"
pkill -f -- "$PATTERN" 2>/dev/null || true
for _ in $(seq 20); do ss -ltn | grep -q ":$PORT " || break; sleep 0.5; done
(MOZ_HEADLESS=1 flatpak run --env=MOZ_HEADLESS=1 org.zotero.Zotero -no-remote -profile "$DIR/profile" > "$DIR/zotero.log" 2>&1 &)
for _ in $(seq 90); do
  "$(dirname "$0")/zeval.sh" 'await Zotero.initializationPromise; return Zotero.version' 2>/dev/null | grep -q '"ok":true' && { echo "test Zotero up (port $PORT)"; exit 0; }
  sleep 1
done
echo "test Zotero did not come up; see $DIR/zotero.log" >&2; exit 1
