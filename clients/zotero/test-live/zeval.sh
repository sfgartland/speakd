#!/usr/bin/env bash
# Evaluate an async JS function body in the test Zotero, in a plugin-like
# chrome sandbox: zeval.sh 'return Zotero.version'
# ZOTERO_TEST_DIR picks the test profile (default ~/.cache/speakd-zotero-test);
# its port and bridge token are read from there.
set -u
DIR="${ZOTERO_TEST_DIR:-$HOME/.cache/speakd-zotero-test}"
PORT="$(cat "$DIR/port" 2>/dev/null || echo 23129)"
TOKEN="$(cat "$DIR/profile/speakd-test-token" 2>/dev/null)" || { echo "no bridge token in $DIR: run setup-profile.sh" >&2; exit 2; }
# The token goes on stdin (-H @-), not the command line, where any user's ps would see it.
printf 'X-Speakd-Test-Token: %s\n' "$TOKEN" | curl -s -m "${ZEVAL_TIMEOUT:-60}" -H @- \
  -H "Content-Type: text/plain" -H "Zotero-Allowed-Request: 1" \
  --data-binary "$1" "http://127.0.0.1:$PORT/speakd-test/eval"
