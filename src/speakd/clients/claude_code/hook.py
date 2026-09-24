"""The one command every Claude Code hook runs.

Two events, and neither of them carries a word of the answer. Prose is the
follower's work: it sees a message land on disk before the tool after it has
even started, where a hook could not fire until that tool had finished. What
is left here is what only a hook can know — that a prompt was submitted, and
what a notification said — and both of those happen once per turn rather than
once per tool call.

Nothing here decides how text should sound — markdown, pronunciation and
summarising are daemon transforms — so this module is only: read stdin, work
out what happened, send it, and under no circumstances fail the user's turn.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from speakd.clients.claude_code import registry
from speakd.clients.claude_code.watermark import state_dir
from speakd.clients.log import logger_for
from speakd.clients.mcp.session import agent_pid, ancestors
from speakd.clients.send import enqueue, hush

# A hush goes out on the UserPromptSubmit path, and Claude Code gives that
# event the shorter window (3s in the manifest this client ships). When there
# were two, one per channel, the pair at `send.TIMEOUT` measured 4.01s against
# it and the prompt stalled on a hook that had already lost. There is one now,
# since a session has one channel; the halved timeout stays, because the
# budget is the user's and interpreter start-up comes out of it too -- which
# test_the_prompt_hushes_fit_inside_the_manifests_budget holds us to.
HUSH_TIMEOUT = 1.0


_log = logger_for(state_dir, "hook.log")


def _channel(session_id: str) -> str:
    """The session's one channel.

    Notifications used to have a second, `:notify`, so that they could be
    heard in a session whose responses were not. A channel's mode now does
    that job -- a notification is `attention`, which every mode speaks -- and
    one row per session in the window is one fewer thing to mute.
    """
    return f"claude-code:{session_id}"


def _claude_pid() -> int:
    """The Claude Code process this hook runs under, for `speakd-mcp` to find.

    Found by `agent_pid`, the same rule the server applies to its own
    ancestry, so the two meet at the same process whatever it is called.
    """
    pid = agent_pid(ancestors())
    if pid is None:
        _log("could not find the Claude Code process above this hook; using the parent")
        return os.getppid()
    return pid


def _dispatch(body: dict[str, object]) -> None:
    session_id = str(body.get("session_id") or "unknown")
    event = str(body.get("hook_event_name") or "")
    channel = _channel(session_id)

    if event == "Notification":
        # Claude is waiting on the user -- for permission, or for input -- and
        # cannot say so itself, so this speaks in every mode.
        message = body.get("message")
        if isinstance(message, str):
            reason = enqueue(channel, message, kind="attention")
            if reason is not None:
                _log(reason)
        return

    if event == "Stop":
        # The backstop for a turn that ended without a briefing. The daemon
        # drops it when the agent already briefed this turn, and in full mode,
        # where the response has just been read aloud.
        reason = enqueue(
            channel,
            "finished",
            kind="attention",
            flags={"unless_briefed": True, "only_in_mode": "brief"},
        )
        if reason is not None:
            _log(reason)
        return

    if event == "UserPromptSubmit":
        transcript_path = body.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            # Before the hushes, and to a file rather than over the socket.
            # This is the one hook left inside the user's critical path: a
            # local write costs microseconds, where a round trip costs
            # milliseconds against a daemon that answers and a full hush
            # timeout against one that does not -- and a registration written
            # after that is one a hook killed at its budget never writes at
            # all, leaving the follower unaware of the session.
            registry.register(
                session_id,
                Path(transcript_path),
                str(body.get("cwd") or ""),
                claude_pid=_claude_pid(),
            )
        reason = hush(channel, new_turn=True, timeout=HUSH_TIMEOUT)
        if reason is not None:
            _log(reason)
        return

    # Every other event is deliberately nothing at all, PostToolUse included:
    # an install still carrying the old four-event manifest keeps firing it,
    # and the follower has already said what it would have.


def _read_and_dispatch() -> None:
    """Everything `main` does, with none of the promises it makes."""
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        _log(f"could not read stdin: {exc}")
        return

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log(f"unreadable hook payload ({exc}): {raw[:200]!r}")
        return

    if not isinstance(body, dict):
        _log(f"hook payload was not an object: {raw[:200]!r}")
        return

    try:
        _dispatch(body)
    except Exception as exc:
        _log(f"{body.get('hook_event_name')} hook failed: {exc!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """Always 0. A hook that fails is a hook that breaks someone's editor.

    The inner guards catch what each step is expected to raise; this one
    catches what no step is expected to raise, which is the only kind of
    failure that has ever reached a user. `json.loads` on deeply nested input
    raises RecursionError -- not a JSONDecodeError, not a UnicodeDecodeError,
    and so straight out through a parse guard that names only those two.

    `BaseException`, because the failures worth surviving are not all
    `Exception`: RecursionError happens to be one, MemoryError is not.
    KeyboardInterrupt and SystemExit are re-raised, since both mean someone
    or something asked this process to stop and neither is ours to swallow.
    """
    try:
        _read_and_dispatch()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: B036 - the whole point of this level
        _log(f"hook failed: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
