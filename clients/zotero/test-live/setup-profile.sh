#!/usr/bin/env bash
# Make a throwaway Zotero profile for live-testing the plugin, with its OWN
# data directory -- never the real ~/Zotero -- the test bridge installed, and
# optionally the built plugin. Usage: setup-profile.sh [dir]  (default
# ~/.cache/speakd-zotero-test). ZOTERO_TEST_PORT sets the local server's port
# (default 23129), so that two test profiles can run side by side. Then
# run-zotero.sh [dir] to start it headless, and zeval.sh to drive it.
#
# The bridge evaluates what it is sent with chrome privileges, so it answers
# only requests carrying the random token written here, mode 0600, to
# <dir>/profile/speakd-test-token (zeval.sh sends it).
set -eu
DIR="${1:-$HOME/.cache/speakd-zotero-test}"
PORT="${ZOTERO_TEST_PORT:-23129}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/profile/extensions" "$DIR/data"
cat > "$DIR/profile/user.js" <<PREFS
// Throwaway profile for speakd's Zotero plugin tests: its own library.
user_pref("extensions.zotero.dataDir", "$DIR/data");
user_pref("extensions.zotero.useDataDir", true);
user_pref("extensions.zotero.firstRun2", false);
user_pref("extensions.zotero.firstRunGuidance", false);
user_pref("app.update.enabled", false);
user_pref("extensions.zotero.sync.autoSync", false);
user_pref("extensions.autoDisableScopes", 0);
user_pref("xpinstall.signatures.required", false);
// Zotero's local server on its own port, never the real Zotero's 23119.
user_pref("extensions.zotero.httpServer.port", $PORT);
user_pref("extensions.zotero.httpServer.enabled", true);
PREFS
echo "$PORT" > "$DIR/port"
TOKEN_FILE="$DIR/profile/speakd-test-token"
if [ ! -s "$TOKEN_FILE" ]; then
  (umask 077 && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN_FILE")
fi
chmod 600 "$TOKEN_FILE"
(cd "$HERE/bridge" && rm -f "$DIR/profile/extensions/speakd-test-bridge@speakd.xpi" && zip -q -r "$DIR/profile/extensions/speakd-test-bridge@speakd.xpi" manifest.json bootstrap.js authorize.js)
XPI="$HERE/../.scaffold/build/speakd-reader.xpi"
if [ -f "$XPI" ]; then cp "$XPI" "$DIR/profile/extensions/speakd-reader@speakd.xpi"; fi
echo "profile ready in $DIR (local server on $PORT)"
