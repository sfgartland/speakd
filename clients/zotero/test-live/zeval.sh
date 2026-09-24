#!/usr/bin/env bash
# Evaluate an async JS function body in the test Zotero, in a plugin-like
# chrome sandbox: zeval.sh 'return Zotero.version'
curl -s -m "${ZEVAL_TIMEOUT:-60}" -H "Content-Type: text/plain" -H "Zotero-Allowed-Request: 1" \
  --data-binary "$1" http://127.0.0.1:23129/speakd-test/eval
