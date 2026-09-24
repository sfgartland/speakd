#!/usr/bin/env bash
# Make a throwaway Zotero profile for live-testing the plugin, with its OWN
# data directory -- never the real ~/Zotero -- the test bridge installed, and
# optionally the built plugin. Usage: setup-profile.sh [dir]  (default
# ~/.cache/speakd-zotero-test). Then run-zotero.sh to start it headless.
set -eu
DIR="${1:-$HOME/.cache/speakd-zotero-test}"
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
user_pref("extensions.zotero.httpServer.port", 23129);
user_pref("extensions.zotero.httpServer.enabled", true);
PREFS
(cd "$HERE/bridge" && rm -f "$DIR/profile/extensions/speakd-test-bridge@speakd.xpi" && zip -q -r "$DIR/profile/extensions/speakd-test-bridge@speakd.xpi" manifest.json bootstrap.js)
XPI="$HERE/../.scaffold/build/speakd-reader.xpi"
if [ -f "$XPI" ]; then cp "$XPI" "$DIR/profile/extensions/speakd-reader@speakd.xpi"; fi
echo "profile ready in $DIR"
