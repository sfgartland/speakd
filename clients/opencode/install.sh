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

# Copied, not linked, so the skill survives the checkout moving; replaced on
# every run so an updated script reaches the user.
skill_dir="$config_dir/skills/speakd-audiobook"
mkdir -p "$config_dir/skills"
rm -rf "$skill_dir"
cp -R "$root/../claude-code/skills/speakd-audiobook" "$skill_dir"
# Bytecode from running the script in the checkout is not part of the skill.
find "$skill_dir" -name __pycache__ -type d -prune -exec rm -rf {} +

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
printf '  open speakd from the app menu (or: systemctl --user start speakd, or uv run speakd &)\n'
