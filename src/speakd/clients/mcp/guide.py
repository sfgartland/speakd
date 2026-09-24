"""The standing briefing guide every agent is handed.

The user's own file when there is one, so what they want to hear about is
said once and reaches every agent; a built-in default otherwise. Per-session
instructions are not kept here: they are whatever the user tells an agent in
its own conversation, and the guide tells the agent to put those first.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT = """\
Brief the user by voice with the `brief` tool. They are usually doing something else \
and listening, not reading.
- When you finish a task: one or two sentences of outcome (kind "done").
- When you are blocked, something failed, or you had to change plan: what and why \
("problem").
- When you need a decision or answer from them: the question, then end your turn \
("question").
- On tasks longer than about ten minutes: a sentence at real milestones ("progress"). \
Never narrate routine steps, file edits or tool calls.
- Under about 25 words. No markdown, no code, no paths unless essential.
- Follow what the user asks for in this conversation over this guide.
- Call `briefing_status` if unsure of the mode; in "full" mode everything is already \
read aloud, so do not brief."""


def text(config_dir: Path | None = None) -> str:
    """The user's guide from `<config>/speakd/briefing.md`, or the default."""
    base = config_dir or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        found = (base / "speakd" / "briefing.md").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return DEFAULT
    return found or DEFAULT
