#!/usr/bin/env bash
# Install the plugin and the MCP server into OpenCode's global config.
#
# The plugin file is copied into ~/.config/opencode/plugins/, which OpenCode
# loads at startup; the MCP entry is merged into opencode.json (created if
# absent, every other key preserved) with this wrapper's absolute path, so
# the installed config finds the checkout whatever OpenCode's own cwd is.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
plugins_dir="$config_dir/plugins"
config_file="$config_dir/opencode.json"

mkdir -p "$plugins_dir"
cp "$root/plugin/speakd.js" "$plugins_dir/speakd.js"

python3 - "$config_file" "$root/speakd-mcp.sh" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
wrapper = str(Path(sys.argv[2]).resolve())
body: dict = {}
if config_path.exists():
    body = json.loads(config_path.read_text(encoding="utf-8"))
body.setdefault("mcp", {})
body["mcp"]["speakd"] = {
    "type": "local",
    "command": ["bash", wrapper],
    "enabled": True,
}
config_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
print(f"wrote {config_path}")
PY

printf 'speakd for OpenCode installed:\n'
printf '  plugin:  %s/speakd.js\n' "$plugins_dir"
printf '  mcp:     speakd in %s\n' "$config_file"
printf 'Restart OpenCode for the plugin to load. The daemon itself: \n'
printf '  systemctl --user enable --now speakd   (or: uv run speakd &)\n'
