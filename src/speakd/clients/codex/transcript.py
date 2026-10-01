"""Read completed assistant messages from Codex rollout JSONL.

The rollout is an internal format, not a stable API. Read only canonical
response items: event messages can repeat the same text, and reasoning and
tool items are never speech. Unknown records are skipped, not guessed at.
"""

from __future__ import annotations

import json


def parse(chunk: bytes) -> tuple[str, int]:
    """Return prose and consumed bytes, leaving a trailing partial line alone."""
    texts: list[str] = []
    consumed = 0
    for raw in chunk.split(b"\n")[:-1]:
        consumed += len(raw) + 1
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(body, dict) or body.get("type") != "response_item":
            continue
        item = body.get("payload")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        if item.get("phase") not in (None, "commentary", "final"):
            continue
        if item.get("recipient") not in (None, "all"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return "\n\n".join(texts), consumed
