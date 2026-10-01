#!/usr/bin/env bash
# Register speakd's Codex hooks and MCP server in the user's Codex home.
set -euo pipefail

client_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
hooks_file="$codex_home/hooks.json"

command -v codex >/dev/null || { echo "codex CLI is required for MCP registration" >&2; exit 1; }
mkdir -p "$codex_home"

python3 - "$hooks_file" "$client_dir/hooks/speakd-hook.sh" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
wrapper = str(Path(sys.argv[2]).resolve())
command = "bash '" + wrapper.replace("'", "'\"'\"'") + "'"
config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
if not isinstance(config, dict):
    raise ValueError(f"{config_path}: expected a JSON object")
hooks = config.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise ValueError(f"{config_path}: hooks must be an object")

for event, timeout in (
    ("UserPromptSubmit", 3),
    ("Interrupt", 3),
    ("PermissionRequest", 5),
    ("Stop", 5),
):
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise ValueError(f"{config_path}: hooks.{event} must be an array")
    own = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if own not in entries:
        entries.append(own)

fd, temporary = tempfile.mkstemp(prefix=".hooks.", suffix=".json", dir=config_path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    os.replace(temporary, config_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(f"wrote {config_path}")
PY

codex mcp add speakd -- bash "$client_dir/speakd-mcp.sh"
printf 'speakd for Codex installed. Restart Codex and the speakd daemon.\n'
