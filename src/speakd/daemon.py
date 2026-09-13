"""The daemon: one process owning the engine, the queue and the channels.

Speech runs on a worker thread consuming a job queue, so the control path
never waits on audio. Arbitration is the scheduler's deterministic rules —
this module obeys them and does not deliberate.

`ProfileView` is how profiles reach the daemon without the daemon knowing what
a profile is. The plugin host supplies the real resolver; a test supplies three
fields and a function.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from traceback import format_exc

from speakd.channels import ChannelTable
from speakd.events import Event, EventBus
from speakd.model import Piece, Role, Span
from speakd.pipeline import speak
from speakd.player import Player
from speakd.protocol import Request, Response, Verb
from speakd.scheduler import SpeechRequest, decide
from speakd.synth import Synthesizer

Prepare = Callable[[Sequence[Piece]], tuple[list[Piece], list[str]]]

_NOT_IMPLEMENTED = (
    "not implemented until the streaming player lands: Player.play blocks, so "
    "there is no way to suspend or seek mid-segment"
)

_NOT_RUNNING = "the daemon is not running: start() it before enqueuing speech"

_DISCARDED = "discarded unspoken: the daemon stopped before this reached the engine"


@dataclass(frozen=True)
class ProfileView:
    """What the daemon needs from a profile, without importing profiles."""

    voice: str
    speed: float
    interrupt_on: tuple[str, ...]
    prepare: Prepare


@dataclass
class _Job:
    source_id: str
    text: str
    profile: ProfileView
    prefix: str


class Daemon:
    def __init__(
        self,
        engine: Synthesizer,
        player: Player,
        profile_for: Callable[[str], ProfileView],
        bus: EventBus | None = None,
        channels: ChannelTable | None = None,
    ) -> None:
        self.engine = engine
        self.player = player
        self.profile_for = profile_for
        self.bus = bus if bus is not None else EventBus()
        self.channels = channels if channels is not None else ChannelTable()
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        # Work accepted but not yet finished — queued jobs and the one in
        # flight alike. Guarded by `_idle`'s lock so that a waiter and the
        # worker's completion are observed together rather than separately;
        # see `wait_idle`.
        self._idle = threading.Condition()
        self._pending = 0
        self._cancel = threading.Event()

    def start(self) -> None:
        # Idempotent: a second worker on the same queue would take alternate
        # jobs and play them over the first worker's audio.
        if self._worker is not None:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, name="speakd-speech", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        # Under the lock, so an enqueue in flight on another thread either
        # lands before this (and is discarded with an error) or is refused.
        with self._idle:
            self._running = False
        worker = self._worker
        if worker is None:
            return
        # Cleared before the join so a second stop() is a no-op rather than a
        # second sentinel left in the queue for a later start() to trip over.
        self._worker = None
        self._cancel.set()
        self._jobs.put(None)
        worker.join(timeout=5.0)

    def wait_idle(self, timeout: float) -> bool:
        """Block until every accepted utterance has finished. Tests use it.

        A job counts as outstanding from the moment `handle` accepts it until
        `_speak` returns, so there is no window in which the daemon looks idle
        with work still queued or in flight. An Event plus a queue check
        cannot say that: the queue is empty for the whole time a job is being
        spoken, and the event can be set by a worker finishing job N a moment
        after the control thread cleared it for job N+1.
        """
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def handle(self, request: Request) -> Response:
        if request.verb is Verb.ENQUEUE:
            return self._enqueue(request)
        if request.verb in (Verb.HUSH, Verb.CANCEL):
            self._cancel.set()
            self.player.stop()
            return Response(ok=True)
        if request.verb in (Verb.PAUSE, Verb.RESUME, Verb.SEEK):
            return Response(ok=False, error=_NOT_IMPLEMENTED)
        if request.verb is Verb.SET_ROLE:
            raw = request.payload.get("role")
            try:
                role = Role(raw)
            except ValueError:
                return Response(ok=False, error=f"unknown role {raw!r}")
            self.channels.set_role(request.source_id, role)
            return Response(ok=True)
        if request.verb is Verb.SET_PRIORITY:
            raw_priority = request.payload.get("priority")
            if not isinstance(raw_priority, int):
                return Response(ok=False, error="priority must be an integer")
            self.channels.set_priority(request.source_id, raw_priority)
            return Response(ok=True)
        return Response(ok=False, error=f"{request.verb.value} is not handled here")

    def _enqueue(self, request: Request) -> Response:
        text = request.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return Response(ok=False, error="enqueue needs non-empty text")
        # Refused rather than queued: with no worker to consume it the job
        # would sit there unspoken behind an ok response, which is the one
        # thing speech is never allowed to do.
        if not self._running:
            return Response(ok=False, error=_NOT_RUNNING)
        kind = str(request.payload.get("kind", "response"))
        channel = self.channels.open(request.source_id)
        profile_name = str(request.payload.get("profile", channel.profile))
        profile = self.profile_for(profile_name)
        decision = decide(
            SpeechRequest(request.source_id, text, kind), channel, profile.interrupt_on
        )
        if not decision.speak:
            return Response(ok=True, data={"spoken": False, "reason": decision.reason})
        job = _Job(
            source_id=request.source_id,
            text=text,
            profile=profile,
            prefix=decision.prefix,
        )
        # Counted before the put, so the job is never in the queue while the
        # daemon still looks idle. Both under the lock stop() takes, so a
        # concurrent stop cannot slip its sentinel in between and leave this
        # job queued behind an ok response with no worker left to speak it.
        with self._idle:
            if not self._running:
                return Response(ok=False, error=_NOT_RUNNING)
            self._pending += 1
            self._jobs.put(job)
        return Response(ok=True, data={"spoken": True})

    def _run(self) -> None:
        try:
            self._consume()
        except Exception:
            # Unreachable by design — `_consume` already contains the one
            # failure that is expected. A worker that exits anyway takes every
            # later utterance with it, so it leaves a traceback behind rather
            # than a silent daemon.
            self._publish("error", "", {"message": f"speech worker died: {format_exc()}"})

    def _consume(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._discard_queued()
                return
            try:
                self._speak(job)
            except Exception as exc:
                # The worker dying is the cardinal failure here: the daemon
                # would go quiet forever with nothing to see. A bad profile
                # costs one utterance, not the process.
                self._publish("error", job.source_id, {"message": f"speech failed: {exc}"})
            finally:
                self._finish_one()

    def _discard_queued(self) -> None:
        """Report whatever is still queued when the worker is told to stop."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            self._publish("error", job.source_id, {"message": _DISCARDED})
            self._finish_one()

    def _finish_one(self) -> None:
        with self._idle:
            self._pending -= 1
            if self._pending == 0:
                self._idle.notify_all()

    def _speak(self, job: _Job) -> None:
        if not self._running:
            # stop() can land between this job leaving the queue and the
            # cancel Event below existing, so the Event alone cannot stop it.
            self._publish("error", job.source_id, {"message": _DISCARDED})
            return
        text = f"{job.prefix} {job.text}".strip() if job.prefix else job.text
        pieces, errors = job.profile.prepare([Piece(span=Span(0, len(text)), spoken=text)])
        for message in errors:
            self._publish("error", job.source_id, {"message": message})
        self._publish("started", job.source_id, {"text": text})
        # A fresh Event per utterance: reusing one is how a channel goes
        # permanently mute with an empty timeline and no error.
        cancel = threading.Event()
        self._cancel = cancel
        try:
            result = speak(
                pieces,
                self.engine,
                self.player,
                voice=job.profile.voice,
                speed=job.profile.speed,
                cancel=cancel,
            )
        except Exception as exc:
            # `started` is already out. A subscriber pairing the two would
            # wait for a `finished` that never came, so send both.
            self._publish("error", job.source_id, {"message": f"synthesis failed: {exc}"})
            self._publish("finished", job.source_id, {"cancelled": False, "aborted": True})
            return
        for segment in result.timeline.segments:
            self._publish(
                "position",
                job.source_id,
                {
                    "text": segment.text,
                    "span_start": segment.span.start,
                    "span_end": segment.span.end,
                    "audio_offset": segment.audio_offset,
                    "played_at": segment.played_at,
                },
            )
        for message in result.errors:
            self._publish("error", job.source_id, {"message": message})
        self._publish(
            "finished",
            job.source_id,
            {"cancelled": result.cancelled, "aborted": result.aborted},
        )

    def _publish(self, kind: str, source_id: str, data: dict[str, object]) -> None:
        self.bus.publish(Event(kind=kind, source_id=source_id, data=data))
