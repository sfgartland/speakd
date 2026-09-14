"""The one command every Claude Code hook runs.

Four events, one code path for two of them. Nothing here decides how text
should sound — markdown, pronunciation and summarising are daemon
transforms — so this module is only: read stdin, work out what is new, send
it, and under no circumstances fail the user's turn.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.send import enqueue, hush
from speakd.clients.claude_code.watermark import load, locked, save, state_dir

NOTIFY_PRIORITY = 10


def _log(message: str) -> None:
    """Append one line to the hook log, or give up quietly.

    Giving up quietly is deliberate: if the log is unwritable there is
    nowhere left to report to, and failing the turn to announce it is the
    worse trade.
    """
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with (directory / "hook.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _channels(session_id: str) -> tuple[str, str]:
    return f"claude-code:{session_id}", f"claude-code:{session_id}:notify"


def _speak_new(session_id: str, transcript_path: str) -> None:
    """Send whatever this session has not spoken, exactly once.

    The lock spans read, send and save. Claude Code fires PostToolUse
    concurrently for parallel tool calls and Stop can overlap one; two
    holders that each read the watermark before either wrote it would both
    send the same text.

    `new_text` returns an empty string with a real watermark when everything
    new was a sub-agent's: the enqueue is then a no-op but the save is not,
    and skipping it would leave those records to be rescanned on every hook
    for the rest of the session.
    """
    response, _ = _channels(session_id)
    with locked(session_id):
        text, mark = new_text(Path(transcript_path), load(session_id))
        if mark is None:
            return
        reason = enqueue(response, text)
        if reason is not None:
            _log(reason)
        save(session_id, mark)


def _dispatch(body: dict[str, object]) -> None:
    session_id = str(body.get("session_id") or "unknown")
    event = str(body.get("hook_event_name") or "")
    response, notify = _channels(session_id)

    if event in ("Stop", "PostToolUse"):
        transcript_path = body.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            _speak_new(session_id, transcript_path)
        return

    if event == "Notification":
        message = body.get("message")
        if isinstance(message, str):
            reason = enqueue(notify, message, priority=NOTIFY_PRIORITY)
            if reason is not None:
                _log(reason)
        return

    if event == "UserPromptSubmit":
        for channel in (response, notify):
            reason = hush(channel)
            if reason is not None:
                _log(reason)
        return


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
