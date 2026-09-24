#!/usr/bin/env bash
# Start (or restart) the headless test Zotero on a profile made by setup-profile.sh.
# Stops only that profile's instance: the pattern names the test profile path,
# and the brackets keep pkill from matching this script's own command line.
set -u
DIR="${1:-$HOME/.cache/speakd-zotero-test}"
pkill -f "[s]peakd-zotero-test/profile" 2>/dev/null || true
for _ in $(seq 20); do ss -ltn | grep -q ":23129 " || break; sleep 0.5; done
(MOZ_HEADLESS=1 flatpak run --env=MOZ_HEADLESS=1 org.zotero.Zotero -no-remote -profile "$DIR/profile" > "$DIR/zotero.log" 2>&1 &)
for _ in $(seq 90); do
  "$(dirname "$0")/zeval.sh" 'await Zotero.initializationPromise; return Zotero.version' 2>/dev/null | grep -q '"ok":true' && { echo "test Zotero up"; exit 0; }
  sleep 1
done
echo "test Zotero did not come up; see $DIR/zotero.log" >&2; exit 1
