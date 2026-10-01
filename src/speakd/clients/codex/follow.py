"""Follow only Codex rollouts registered by a user prompt, never scan history."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import FrameType

from speakd.clients import registry
from speakd.clients.codex import state
from speakd.clients.codex.transcript import parse
from speakd.clients.log import logger_for
from speakd.clients.send import send as send_request
from speakd.protocol import Request, Verb

Send = Callable[[str, str, dict[str, object]], str | None]
_log = logger_for(state.state_dir, "hook.log")


def _send(verb: str, source: str, payload: dict[str, object]) -> str | None:
    # Prompt/interrupt hooks wait at most 0.65s for our lock. Keep the socket
    # call shorter so a missing daemon cannot turn a new prompt into a stall.
    return send_request(Request(Verb(verb), source, payload), timeout=0.5)


class Follower:
    def __init__(self, *, send: Send | None = None, poll_seconds: float = 0.1) -> None:
        self._send = send if send is not None else _send
        self.poll_seconds = poll_seconds
        self._labels: dict[str, str] = {}

    def tick(self) -> None:
        for reg in registry.live():
            if reg.client != "codex":
                continue
            try:
                self._follow(reg)
            except TimeoutError:
                continue  # A prompt hook owns this session; retry next tick.
            except Exception as exc:
                _log(f"follower: session {reg.session_id}: {exc}")

    def _follow(self, reg: registry.Registration) -> None:
        channel = f"codex:{reg.session_id}"
        with state.locked(reg.session_id):
            cursor = state.load(reg.session_id)
            if cursor is None or cursor.path != str(reg.transcript):
                # Recover from the prompt boundary, not from a later EOF that
                # may already contain the answer. Legacy registrations have
                # no boundary; avoid replaying their history.
                offset = reg.start_offset
                if offset is None:
                    offset = reg.transcript.stat().st_size
                cursor = state.Cursor(str(reg.transcript), offset)
                state.save(reg.session_id, cursor)
            if not cursor.active:
                return
            size = reg.transcript.stat().st_size
            offset = cursor.offset if cursor.offset <= size else 0
            with reg.transcript.open("rb") as handle:
                handle.seek(offset)
                text, consumed = parse(handle.read())
            if text:
                reason = self._send("enqueue", channel, {"text": text, "kind": "response"})
                if reason is not None:
                    _log(f"follower: {reason}")
                    return
            if consumed or offset != cursor.offset:
                state.save(reg.session_id, replace(cursor, offset=offset + consumed))
        # Labels do not need the cursor lock; keep prompt interruption quick.
        name = Path(reg.cwd).name if reg.cwd else reg.session_id[:8]
        label = f"Codex · {name}"
        if self._labels.get(reg.session_id) != label:
            if self._send("set_label", channel, {"label": label}) is None:
                self._labels[reg.session_id] = label

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self.poll_seconds)


def main() -> int:
    stop = threading.Event()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    Follower().run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
