"""Fail-open Codex hooks. Only the prompt hook emits model context on stdout."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from speakd.clients import registry
from speakd.clients.codex import state
from speakd.clients.log import logger_for
from speakd.clients.mcp.session import agent_pid, ancestors
from speakd.clients.send import send
from speakd.protocol import Request, Verb

_log = logger_for(state.state_dir, "hook.log")


def _send(verb: str, source: str, payload: dict[str, object]) -> None:
    reason = send(Request(Verb(verb), source, payload), timeout=0.5)
    if reason:
        _log(reason)


def dispatch(body: dict[str, object]) -> dict[str, object] | None:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    channel = f"codex:{session_id}"
    event = body.get("hook_event_name")
    if event == "UserPromptSubmit":
        path = body.get("transcript_path")
        # Hold the same lock as the follower: no old text can be enqueued
        # between resetting the cursor and silencing the previous turn.
        with state.locked(session_id, timeout=0.65):
            if isinstance(path, str) and path:
                transcript = Path(path)
                try:
                    offset = transcript.stat().st_size
                except FileNotFoundError:
                    offset = 0
                state.save(session_id, state.Cursor(path, offset))
                registry.register(
                    session_id,
                    transcript,
                    str(body.get("cwd") or ""),
                    client="codex",
                    agent_pid=agent_pid(ancestors()),
                )
            else:
                cursor = state.load(session_id)
                if cursor is not None:
                    state.save(session_id, replace(cursor, active=False))
            _send("hush", channel, {"new_turn": True})
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"Your speakd speech channel is {channel}. Pass source_id={channel!r} "
                    "on every speakd MCP call (brief, briefing_status, set_mode) so it reaches "
                    "this conversation, even when other Codex conversations share the host."
                ),
            }
        }
    if event == "Interrupt":
        with state.locked(session_id, timeout=0.65):
            cursor = state.load(session_id)
            if cursor is not None:
                state.save(session_id, replace(cursor, active=False))
            _send("hush", channel, {})
    elif event == "PermissionRequest":
        _send("enqueue", channel, {"text": "Codex needs your permission.", "kind": "attention"})
    elif event == "Stop":
        _send(
            "enqueue",
            channel,
            {
                "text": "finished",
                "kind": "attention",
                "unless_briefed": True,
                "only_in_mode": "brief",
            },
        )
    return None


def main() -> int:
    try:
        body = json.loads(sys.stdin.read())
        if isinstance(body, dict):
            output = dispatch(body)
            if output is not None:
                print(json.dumps(output))
    except Exception as exc:
        _log(f"Codex hook: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
