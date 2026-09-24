#!/usr/bin/env bash
# This checkout's daemon with a fake engine, isolated: socket under
# $SPEAKD_TEST_RUNTIME (default /tmp/speakd-zotero-test; keep it short -- AF_UNIX paths are limited), HTTP on
# 127.0.0.1:8743, token in <dir>/config/speakd/http-token.
set -u
DIR="${1:-$HOME/.cache/speakd-zotero-test}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="${SPEAKD_TEST_RUNTIME:-/tmp/speakd-zotero-test}"
mkdir -p "$RUN/speakd" "$DIR/config" "$DIR/state"
export XDG_RUNTIME_DIR="$RUN" XDG_CONFIG_HOME="$DIR/config" XDG_STATE_HOME="$DIR/state"
export SPEAKD_HTTP_PORT=8743 SPEAKD_NO_FOLLOWER=1 SPEAKD_NO_NOTIFY=1
cd "$HERE/../../.."
exec uv run python "$HERE/fakedaemon.py" "$RUN/speakd/speakd.sock"
