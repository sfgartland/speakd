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
from speakd.player import Pausable, Player
from speakd.protocol import Request, Response, Verb
from speakd.scheduler import SpeechRequest, decide
from speakd.synth import Synthesizer

Prepare = Callable[[Sequence[Piece]], tuple[list[Piece], list[str]]]

# Named for the one verb still refused. The wording it replaces — "not
# implemented until the streaming player lands" — became false the moment the
# streaming player landed, and a refusal whose stated reason has already
# happened sends a reader waiting for something that is already here.
_SEEK_NOT_IMPLEMENTED = (
    "seek is not implemented: the timeline can map an offset to a time, but "
    "moving playback needs the audio for segments already played, which is "
    "neither retained nor re-synthesised"
)

_NOT_RUNNING = "the daemon is not running: start() it before enqueuing speech"

_STILL_FINISHING = (
    "a previous speech worker has not finished yet: stop() timed out waiting for it, "
    "and a second worker on the same queue would play over the first"
)

# Two causes, two messages. One text for both told a user who hushed that the
# daemon had stopped — false about a daemon still running, and exactly what
# sends a fresh reader hunting a shutdown that never happened.
_DISCARDED_STOPPED = "discarded unspoken: the daemon stopped before this reached the engine"

_DISCARDED_HUSHED = "discarded unspoken: a hush cleared the queue before this was spoken"


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

    def start(self) -> Response:
        """Bring the speech worker up, saying which of three things happened.

        Supervision needs to tell "already running" from "refused, a previous
        worker is still finishing": the first is nothing to do, the second is
        worth retrying.
        """
        with self._idle:
            if self._worker is not None:
                if self._running:
                    return Response(ok=True, data={"started": False, "reason": "already running"})
                # A worker that outlived stop()'s join is still holding the
                # queue. A second consumer would take alternate jobs and play
                # them over the first worker's audio, so this is refused —
                # and refused visibly, because retrying later is right.
                return Response(ok=False, error=_STILL_FINISHING)
            self._running = True
            worker = threading.Thread(target=self._run, name="speakd-speech", daemon=True)
            # Published before the thread runs, so `_retire`'s identity check
            # can never fail to recognise the worker it belongs to.
            self._worker = worker
            try:
                # Started under the same lock that published it, so no other
                # thread can see `_worker` before the thread is running. A
                # stop() landing here waits for start() to finish instead of
                # joining a thread that was never started -- a RuntimeError
                # out of stop(), and out of serve() as a traceback.
                # `Thread.start()` blocks only on its own `_started` event and
                # never re-enters the daemon, so holding the lock is safe.
                worker.start()
            except BaseException:  # noqa: B036 - rolled back, then re-raised
                # "can't start new thread" would otherwise leave a daemon that
                # accepts speech with nothing to consume it and no way back:
                # with `_worker` set, every later start() quietly no-ops.
                if self._worker is worker:
                    self._worker = None
                    self._running = False
                raise
        return Response(ok=True, data={"started": True})

    def silence(self) -> None:
        """Stop sounding now, and take no more speech. The teardown is `stop()`.

        A shutdown wants the audio to end the moment it is asked for, but
        `stop()` joins its worker for up to 5s and a server has to be closed
        first: `SocketServer.stop()`'s shutdown() is what frees a worker
        wedged writing to a subscriber, so it cannot be made to wait behind
        that join. This is the part of `stop()` that is immediate.
        `_running` goes with the cancel so that nothing queued behind the
        cancelled utterance starts speaking in the meantime.
        """
        with self._idle:
            self._running = False
            self._cancel.set()
        self._stop_player()

    def stop(self) -> bool:
        """Take the worker down, saying whether it actually left.

        The answer matters to whoever owns the audio device. The join below
        times out rather than blocking forever, and this returns either way —
        so "the worker has finished" is something a caller must ask, not
        assume. A caller that frees a device under a worker still inside
        `play()` gets undefined behaviour out of the sound library, not an
        exception it can catch.

        True when there was no worker or the join succeeded; False when the
        worker outlived it and is still running.
        """
        # Under the lock, so an enqueue in flight on another thread either
        # lands before this (and is discarded with an error) or is refused.
        with self._idle:
            self._running = False
            worker = self._worker
            # Set under the lock that guards it, as in the HUSH/CANCEL path
            # and for the same reason: released first, the worker could
            # finish this utterance and install the next job's Event in
            # between, leaving this set() on an Event nobody is watching
            # while that next job speaks on. `Event.set()` never blocks, so
            # holding the lock across it costs nothing.
            self._cancel.set()
        if worker is None:
            return True
        # Symmetric with the HUSH/CANCEL path, and for the same reason: the
        # Event alone stops nothing that is already sounding. A worker cannot
        # leave mid-`play()`, and a real player's `play()` blocks for the
        # length of the segment, so without this the join below waits out the
        # sentence and then times out — leaving stop() to return while the
        # daemon is still audibly speaking, and a worker behind it that makes
        # the next start() refuse. Outside the lock, as there, because this
        # is the one part that can block: player.stop() talks to a real
        # device. No further ordering is needed — a job that reaches `_speak`
        # after this rechecks `_running` and is discarded before it ever
        # reaches the player.
        self._stop_player()
        with self._idle:
            # Under the lock, and only while this is still the worker being
            # stopped. `_retire` clears `_worker` and empties the queue under
            # the same lock, so the sentinel either lands in time to be taken
            # with everything else or is never deposited. Outside the lock it
            # could land just after a retiring worker drained — unowned, for
            # the next worker to eat and close the daemon with.
            if self._worker is worker:
                self._jobs.put(None)
        worker.join(timeout=5.0)
        if worker.is_alive():
            # The join timed out. `_worker` deliberately keeps pointing at it:
            # a worker still holding the queue must not be replaced, and it
            # retires itself — sentinel and all — when it finally gets out.
            # Clearing here instead would let the next start() spawn a rival
            # consumer, and leave a sentinel to close it again immediately.
            return False
        with self._idle:
            if self._worker is worker:
                self._worker = None
        return True

    def _stop_player(self) -> None:
        """Silence the device, reporting rather than raising if it refuses.

        A real sink's stop() can fail — the headset walking out of range is
        the case `pipeline` already anticipates. Raised out of `stop()` it
        would escape before the sentinel is deposited: the worker would never
        be told to leave, `_worker` would keep pointing at it, and every later
        start() would return `_STILL_FINISHING`. Reported on the bus instead,
        like every other sink failure in this module.
        """
        try:
            self.player.stop()
        except Exception as exc:
            self._publish("error", "", {"message": f"could not silence the player: {exc!r}"})

    def _set_paused(self, paused: bool) -> Response:
        """Suspend or take up playback, announcing only a real change.

        A player that cannot pause is not an error in the daemon; it is a
        fact about how this daemon was constructed, and the caller is told
        which player it got so the answer is actionable.

        The state goes on the bus because a GUI must be able to *read* it
        rather than infer it from the absence of `position` events — silence
        is also what a finished utterance sounds like. Only a real change is
        announced: a pause when already paused is a no-op, and publishing it
        would make a GUI that redraws on every event flicker.
        """
        player = self.player
        if not isinstance(player, Pausable):
            return Response(
                ok=False,
                error=f"{type(player).__name__} cannot pause",
            )
        if player.paused == paused:
            return Response(ok=True, data={"paused": paused})
        verb = "pause" if paused else "resume"
        try:
            if paused:
                player.pause()
            else:
                player.resume()
        except Exception as exc:
            # Guarded like every other player call here: a sink that refuses
            # must not raise out of `handle` as a traceback on the control
            # path. Nothing is announced, because nothing changed — a
            # `transport` event for a pause that did not happen is worse than
            # none, since it is exactly what a GUI trusts.
            return Response(ok=False, error=f"could not {verb} the player: {exc!r}")
        self._publish("transport", "", {"paused": paused})
        return Response(ok=True, data={"paused": paused})

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
            # Both stop what is being said; they differ in what follows it.
            # `cancel` skips this one utterance and lets the queue run on —
            # an agent superseding its own announcement, which is the ordinary
            # case for one that changes its mind mid-task. `hush` means stop
            # talking, so the queue goes with it. Each reports what it did, so
            # a client can tell them apart from the response alone.
            #
            # The Event is read AND set under the lock. Set outside it, the
            # worker could finish this utterance, take the next job off the
            # queue and install its fresh Event in between — leaving this
            # set() on an Event nobody is watching, the drain below finding
            # an already-empty queue, and that next job speaking on after
            # hush answered `ok`. `Event.set()` never blocks. Only
            # `player.stop()` does, on a real device, which is why that one
            # stays outside: holding the lock across it would stall enqueue.
            # This narrows the window rather than closing it; `_speak` says
            # what is left of it.
            with self._idle:
                self._cancel.set()
            try:
                self.player.stop()
            finally:
                # In a finally, because a sink that refuses to stop — the
                # headset out of range again — must not carry the drain off
                # with it. Without this, a hush that met a failing player
                # reported an error and left every queued utterance in place:
                # the daemon answering a request to stop talking by going on
                # talking. The failure still reaches the caller; only the
                # drain is no longer hostage to it.
                discarded = self._drain_queued() if request.verb is Verb.HUSH else 0
            return Response(ok=True, data={"discarded": discarded})
        if request.verb is Verb.STATUS:
            # Read-only, deliberately: asking what the channels are must not
            # open one for the asker. `speakctl status` would otherwise leave
            # a phantom "cli" channel in every listing it printed.
            return Response(
                ok=True,
                data={
                    "channels": [
                        {
                            "source_id": c.source_id,
                            "role": c.role.value,
                            "priority": c.priority,
                            "profile": c.profile,
                            "label": c.label,
                        }
                        for c in self.channels.all()
                    ]
                },
            )
        if request.verb in (Verb.PAUSE, Verb.RESUME):
            return self._set_paused(request.verb is Verb.PAUSE)
        if request.verb is Verb.SEEK:
            return Response(ok=False, error=_SEEK_NOT_IMPLEMENTED)
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
            # bool is a subclass of int: JSON `true` would otherwise silently
            # set priority 1, which is a real standing, not a no-op.
            if not isinstance(raw_priority, int) or isinstance(raw_priority, bool):
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
            # On the bus as well as in the response: a GUI watching the event
            # stream has to be able to show why nothing was said.
            self._publish(
                "declined",
                request.source_id,
                {"text": text, "kind": kind, "reason": decision.reason},
            )
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
        obituary = ""
        try:
            self._consume()
        except BaseException:  # noqa: B036 - carried out as the obituary below
            # Unreachable by design — `_consume` already contains the one
            # failure that is expected. BaseException rather than Exception
            # because `threading` discards a SystemExit raised on a worker
            # thread without so much as a traceback, and a plugin that runs
            # argparse raises exactly that.
            obituary = format_exc()
        finally:
            # However the worker leaves, it closes the daemon behind it. A
            # daemon with no worker that still answers `ok` to enqueue is the
            # silent-loss failure this module exists to prevent.
            self._retire(obituary)

    def _retire(self, obituary: str = "") -> None:
        """Shut the door: refuse new speech, report what was queued, wake waiters."""
        jobs: list[_Job] = []
        with self._idle:
            # Only the worker the daemon currently owns may close it. One that
            # outlived a stop()'s join speaks for nobody, and retiring here
            # would shut down a daemon its successor is running.
            ours = self._worker is threading.current_thread()
            if ours:
                self._running = False
                self._worker = None
                # Emptied under the lock that clears `_worker`, and reported
                # once it is released: a concurrent stop() either gets its
                # sentinel in before this and it is taken here, or finds the
                # worker already gone and deposits none.
                jobs, _signals = self._empty_queue()
        if ours:
            try:
                for job in jobs:
                    self._publish("error", job.source_id, {"message": _DISCARDED_STOPPED})
            finally:
                with self._idle:
                    # Whatever the bookkeeping did on the way down, nothing is
                    # in flight now: a waiter must not block on a phantom job.
                    self._pending = 0
                    self._idle.notify_all()
        if obituary:
            # Last, and never before `_worker` is cleared. `EventBus.publish`
            # is synchronous, so a supervisor subscribing to this runs on the
            # dying worker's own stack: announcing the death any earlier means
            # its start() finds a worker still in place and quietly no-ops.
            self._publish("error", "", {"message": f"speech worker died: {obituary}"})

    def _consume(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                # Retirement, including the drain, happens in `_run`'s finally
                # — one path down whether the worker was stopped or died.
                return
            try:
                self._speak(job)
            except BaseException as exc:  # noqa: B036 - one utterance, not the process
                # The worker dying is the cardinal failure here: the daemon
                # would go quiet forever with nothing to see. A bad profile
                # costs one utterance, not the process — including a profile
                # that raises SystemExit, which `except Exception` lets past.
                self._publish("error", job.source_id, {"message": f"speech failed: {exc!r}"})
            finally:
                self._finish_one()

    def _empty_queue(self) -> tuple[list[_Job], int]:
        """Take everything off the queue: the jobs, and how many stop signals."""
        jobs: list[_Job] = []
        signals = 0
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return jobs, signals
            if item is None:
                signals += 1
            else:
                jobs.append(item)

    def _drain_queued(self) -> int:
        """Drop everything queued and report it, returning how many there were.

        Taken off the queue in one step under the lock, with any stop signal
        put straight back, and only then reported. `EventBus.publish` swallows
        a subscriber's `Exception` but not its `BaseException`, and one
        escaping must not leave jobs off the queue with their pending count
        unreleased — `wait_idle` would block to its timeout — nor swallow a
        sentinel `stop()` is waiting on.

        One event for the whole drain, not one per job: a hush of forty
        queued utterances is one thing that happened, and forty events is a
        GUI redrawing forty times to say so. It carries the count and the
        channels the count came from, which is what the per-job events said
        between them.
        """
        with self._idle:
            jobs, signals = self._empty_queue()
            for _ in range(signals):
                self._jobs.put(None)
        try:
            if jobs:
                # Its own kind, because a GUI showing "3 dropped" had to match
                # on the message text to find them, and prose is not an API:
                # the day someone rewords this, the GUI goes quiet about
                # speech that vanished. No event when nothing was dropped —
                # `discarded: 0` is not news.
                self._publish(
                    "discarded",
                    "",
                    {
                        "count": len(jobs),
                        "reason": _DISCARDED_HUSHED,
                        "sources": sorted({job.source_id for job in jobs}),
                    },
                )
        finally:
            self._release(len(jobs))
        return len(jobs)

    def _finish_one(self) -> None:
        self._release(1)

    def _release(self, count: int) -> None:
        if count <= 0:
            return
        with self._idle:
            # Floored: a double release would otherwise drive the count
            # negative, where it never reaches zero and wait_idle never returns.
            self._pending = max(0, self._pending - count)
            if self._pending == 0:
                self._idle.notify_all()

    def _speak(self, job: _Job) -> None:
        """Say one job, from the cancel Event down to the `finished` event.

        One window is knowingly left open. A hush landing between `_consume`
        taking this job off the queue and the assignment below sets the
        previous utterance's Event and drains a queue this job is no longer
        in, so the job speaks after the hush was answered. It is a few
        bytecodes wide with no I/O in it, where the same hole before the
        review spanned a whole utterance's worth of event publishing. Closing
        it properly wants a hush generation counter — a number bumped by
        every hush, recorded on a job when it is enqueued, and compared here —
        not a wider lock; the lock cannot help, because the job is already out
        of the queue by then.
        """
        # First, before anything else can run. A fresh Event per utterance:
        # reusing one is how a channel goes permanently mute with an empty
        # timeline and no error. Assigned here rather than after `prepare`
        # because `prepare` is plugin code and `started` is a synchronous
        # write to clients: a hush landing in either would otherwise set the
        # previous, already-consumed utterance's Event and be lost.
        cancel = threading.Event()
        with self._idle:
            self._cancel = cancel
            # Read under the same lock that installs the Event, so a stop()
            # cannot land between the two and leave this job installed as the
            # current utterance and past its own liveness check at once.
            running = self._running
        if not running:
            # stop() can still land between this job leaving the queue and
            # the check above, so the Event alone cannot stop it.
            self._publish("error", job.source_id, {"message": _DISCARDED_STOPPED})
            return
        text = f"{job.prefix} {job.text}".strip() if job.prefix else job.text
        pieces, errors = job.profile.prepare([Piece(span=Span(0, len(text)), spoken=text)])
        for message in errors:
            self._publish("error", job.source_id, {"message": message})
        self._publish("started", job.source_id, {"text": text})
        if cancel.is_set():
            # Hushed during `prepare` or by a subscriber of `started` itself.
            # `speak()` would notice this too, but only after synthesising and
            # playing the first segment.
            self._publish("finished", job.source_id, {"cancelled": True, "aborted": False})
            return
        try:
            result = speak(
                pieces,
                self.engine,
                self.player,
                voice=job.profile.voice,
                speed=job.profile.speed,
                cancel=cancel,
            )
        except BaseException as exc:  # noqa: B036 - re-raising would drop `finished`
            # `started` is already out. A subscriber pairing the two would
            # wait for a `finished` that never came, so send both — including
            # when the engine or the player raises outside `Exception`.
            # `!r` because `str(SystemExit(3))` is just "3": a diagnostic
            # that names neither the exception nor its type is no diagnostic.
            self._publish("error", job.source_id, {"message": f"synthesis failed: {exc!r}"})
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
