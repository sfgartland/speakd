"""Speak a Claude Code transcript as it grows.

Claude Code flushes an assistant message's content blocks together, when the
message completes -- measured, not assumed -- so there is no text to be had
mid-sentence and this loop does not pretend otherwise. What it buys over the
hooks it replaces is the gap between a message landing on disk and the tool
after it finishing: `PostToolUse` could not fire until the tool was done, and
on a slow tool that was seconds of silence for text already written.

Polling, not inotify. The transcript only ever grows, so a size check on a
timer is portable and needs no dependency; inotify would tie this to Linux for
nothing a listener could hear.

Nothing raises out of `tick`. One corrupt transcript must not stop every other
session being spoken.
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

from speakd.clients.claude_code import registry
from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.transcript import ai_title, parse
from speakd.clients.claude_code.watermark import Watermark, load, save, state_dir
from speakd.clients.log import logger_for
from speakd.clients.send import send as send_request
from speakd.protocol import Request, Verb

_log = logger_for(state_dir, "hook.log")

DEFAULT_POLL_SECONDS = 0.1

# How far back to look for the session's name. `ai-title` is rewritten as the
# session develops, so only the tail matters, and a bounded read keeps this
# cheap enough to do whenever new text arrives.
TITLE_TAIL_BYTES = 64 * 1024

Send = Callable[[str, str, dict[str, object]], str | None]


def _default_send(verb: str, source: str, payload: dict[str, object]) -> str | None:
    return send_request(Request(verb=Verb(verb), source_id=source, payload=payload))


def _latest_title(path: Path) -> str | None:
    """The session's name, from a bounded read of the transcript's tail."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - TITLE_TAIL_BYTES)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return None
    # Drop a leading partial line: a seek into the middle of one yields bytes
    # that are not JSON, and `parse` would count them as consumed.
    if start and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]
    records, _ = parse(chunk)
    return ai_title(records)


def _label_for(reg: registry.Registration, title: str | None) -> str:
    """What the GUI should show for this session.

    `ai-title` does not appear until Claude Code has named the session, so the
    working directory carries the label until it does, and a short session id
    carries it if even that is missing.
    """
    if title:
        return f"Claude Code · {title}"
    name = Path(reg.cwd).name if reg.cwd else ""
    if name:
        return f"Claude Code · {name}"
    return f"Claude Code · {reg.session_id[:8]}"


class Follower:
    def __init__(
        self,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        send: Send | None = None,
    ) -> None:
        self.poll_seconds = poll_seconds
        self._send: Send = send if send is not None else _default_send
        # Last label sent per session. A label re-sent ten times a second is
        # ten needless round trips saying the same thing.
        self._labels: dict[str, str] = {}

    def tick(self) -> None:
        for reg in registry.live():
            try:
                self._follow(reg)
            except Exception as exc:
                _log(f"follower: session {reg.session_id} failed: {exc!r}")

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self.poll_seconds)

    def _follow(self, reg: registry.Registration) -> None:
        channel = f"claude-code:{reg.session_id}"
        mark = load(reg.session_id)
        if mark is None or mark.path != str(reg.transcript):
            self._start_at_end(reg)
            self._relabel(reg, channel)
            return
        text, new_mark = new_text(reg.transcript, mark)
        if new_mark is None:
            return
        if text.strip():
            reason = self._send("enqueue", channel, {"text": text})
            if reason is not None:
                # Deliberately not saved. The hook this replaces advanced its
                # watermark whether or not the daemon took the text, so a
                # daemon that was briefly unreachable lost that speech for
                # good. Leaving the mark where it is costs a repeat at worst.
                _log(f"follower: {reason}")
                return
        save(reg.session_id, new_mark)
        self._relabel(reg, channel)

    def _start_at_end(self, reg: registry.Registration) -> None:
        """Begin at end of file, so registering speaks no history."""
        try:
            size = reg.transcript.stat().st_size
        except OSError:
            return
        save(reg.session_id, Watermark(path=str(reg.transcript), offset=size, uuid=""))

    def _relabel(self, reg: registry.Registration, channel: str) -> None:
        label = _label_for(reg, _latest_title(reg.transcript))
        if self._labels.get(reg.session_id) == label:
            return
        reason = self._send("set_label", channel, {"label": label})
        if reason is None:
            self._labels[reg.session_id] = label


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="speakd-claude-follow", description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    stop = threading.Event()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    Follower(poll_seconds=args.poll_seconds).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
