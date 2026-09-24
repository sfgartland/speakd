"""The daemon: one process owning the engine, the queue and the channels.

Speech runs on a worker thread consuming a job queue, so the control path
never waits on audio. Arbitration is the scheduler's deterministic rules —
this module obeys them and does not deliberate.

`ProfileView` is how profiles reach the daemon without the daemon knowing what
a profile is. The plugin host supplies the real resolver; a test supplies three
fields and a function.
"""

from __future__ import annotations

import math
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from traceback import format_exc
from typing import Protocol, runtime_checkable

from speakd import segmenter, state
from speakd.channels import ChannelTable
from speakd.events import Event, EventBus
from speakd.metrics import SynthesisWindow, resident_bytes
from speakd.model import Piece, Role, Segment, Span
from speakd.pipeline import AudioCache, speak
from speakd.player import Pausable, Player
from speakd.protocol import Request, Response, Verb
from speakd.scheduler import SpeechRequest, decide
from speakd.synth import Synthesizer
from speakd.tempo import Tempo
from speakd.timeline import Timeline

Prepare = Callable[[Sequence[Piece]], tuple[list[Piece], list[str]]]

_NOT_RUNNING = "the daemon is not running: start() it before enqueuing speech"

_METRICS_THREAD_NAME = "speakd-metrics"

_ENGINE_THREAD_NAME = "speakd-engine-load"

# How often the monitor's numbers go out while speech is in flight. A second
# is what a pinned display refreshes at; faster would be traffic nobody reads,
# and slower would show a stall after it had already been heard.
_METRICS_INTERVAL_SECONDS = 1.0

# How long stop() waits for the ticker to leave. It is woken by an Event
# rather than the interval, so this is the length of one publish to a slow
# subscriber, not of a tick.
_JOIN_METRICS_SECONDS = 2.0

_STILL_FINISHING = (
    "a previous speech worker has not finished yet: stop() timed out waiting for it, "
    "and a second worker on the same queue would play over the first"
)

# Two causes, two messages. One text for both told a user who hushed that the
# daemon had stopped — false about a daemon still running, and exactly what
# sends a fresh reader hunting a shutdown that never happened.
_DISCARDED_STOPPED = "discarded unspoken: the daemon stopped before this reached the engine"

_DISCARDED_HUSHED = "discarded unspoken: a hush cleared the queue before this was spoken"

_DISCARDED_MUTED = "discarded unspoken: a mute cleared the queue before this was spoken"

# How much of a queued utterance a client is shown. Enough to recognise the
# sentence that is coming; short enough that a status response listing a deep
# queue stays a control message rather than a copy of everything waiting to be
# said -- a paragraph the daemon is about to speak is bounded by nothing.
QUEUE_PREVIEW_CHARS = 200


def _preview(text: str) -> str:
    """The head of an utterance, for a client showing what is coming.

    Sliced with nothing appended. An ellipsis would be text the utterance does
    not contain, and the obvious use for this -- matching a preview against
    what was enqueued -- would then have to strip it first.
    """
    return text[:QUEUE_PREVIEW_CHARS]


@runtime_checkable
class Loadable(Protocol):
    """An engine that can be put down and picked up again.

    Separate from `Synthesizer` for the reason `Pausable` is separate from
    `Player`: nothing in the speech path needs an engine to be unloadable,
    and folding these in would make every test double carry four members it
    has no use for. The daemon asks with `isinstance` and refuses
    `set_engine`, naming the engine, when the answer is no — a daemon whose
    engine cannot be put down is a fact about how it was built, not an error
    in it.

    `runtime_checkable` checks only that the attributes exist, not their
    signatures. That is what is wanted here: the question is whether this
    engine can be unloaded at all.
    """

    @property
    def loaded(self) -> bool: ...

    @property
    def loading(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...


@dataclass(frozen=True)
class ProfileView:
    """What the daemon needs from a profile, without importing profiles."""

    voice: str
    speed: float
    interrupt_on: tuple[str, ...]
    prepare: Prepare


@dataclass(frozen=True)
class _Spoken:
    """An utterance as it was prepared for speaking, kept so it can be said again.

    The transforms' output and the segmentation, not only the text: a replay
    that ran `prepare` again could segment differently -- a plugin is free to
    be non-deterministic -- and the index a listener clicked would then name a
    different sentence from the one on their screen.
    """

    source_id: str
    profile: ProfileView
    text: str
    pieces: tuple[Piece, ...]
    units: tuple[Piece, ...]


@dataclass
class _Job:
    source_id: str
    text: str
    profile: ProfileView
    prefix: str
    # Set for a replay: speak this, already prepared, from `start`.
    replay: _Spoken | None = None
    start: int = 0


@dataclass
class _Current:
    """The utterance in flight, as a seek needs to see it.

    `index` is the segment last announced, so a relative seek is relative to
    what a listener was shown rather than to how far ahead the producer has
    got. `seek_to` is a request `_speak` picks up once `speak()` returns;
    anything that stops speech clears it, so a hush racing a seek wins.
    Guarded by the daemon's `_idle` lock.
    """

    units: tuple[Piece, ...]
    index: int = 0
    seek_to: int | None = None


class Daemon:
    def __init__(
        self,
        engine: Synthesizer,
        player: Player,
        profile_for: Callable[[str], ProfileView],
        bus: EventBus | None = None,
        channels: ChannelTable | None = None,
        metrics_interval: float = _METRICS_INTERVAL_SECONDS,
    ) -> None:
        self.engine = engine
        self.player = player
        self.profile_for = profile_for
        self.bus = bus if bus is not None else EventBus()
        self.channels = channels if channels is not None else ChannelTable()
        # Loaded rather than defaulted: after a reboot, surprising silence is
        # a smaller failure than surprising speech.
        self.muted = state.load().muted
        # The listener's speed, read back for the same reason. Shared with
        # the pipeline and the player, which read it as they go: a change
        # reaches the sentence already playing, not only the next one.
        self.tempo = Tempo(state.load().speed)
        # Whose utterance the worker is on, so that a channel-scoped silence
        # can tell whether the voice it would cut off is the one being muted.
        # Stale between jobs in exactly the way `_cancel` is, and harmlessly
        # so: the Event it stands beside has already been consumed, and
        # setting a consumed Event stops nothing.
        self._speaking = ""
        # What a seek moves within; None between utterances.
        self._current: _Current | None = None
        # The last utterance to start speaking, kept after it ends so that a
        # listener can play it again from a sentence of their choosing.
        self._last: _Spoken | None = None
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
        # Accepted work, ever. The metrics ticker compares it against what it
        # last settled on, which is how an utterance that began and ended
        # between two ticks still reaches a monitor. A count rather than a
        # flag the ticker clears: written in one place, never read back here.
        self._accepted = 0
        # What the worker has already written down, read by the ticker. The
        # window outlives an utterance -- it is a trailing measure of the
        # machine, not of this sentence -- where the timeline is the utterance
        # being spoken, or the last one, so that the tick which settles the
        # display carries its final drift rather than a zero.
        self._synthesis = SynthesisWindow()
        self._timeline: Timeline | None = None
        self._metrics_interval = metrics_interval
        self._metrics: threading.Thread | None = None
        self._metrics_stop = threading.Event()

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
            ticker = threading.Thread(
                target=self._tick_metrics,
                # The baseline is read here, under the lock, rather than on
                # the ticker's own first pass: a thread that reads it after
                # `start()` has released this lock can find an enqueue already
                # counted, and then takes the first utterance for work it has
                # already settled -- ticking through it and never settling.
                args=(self._accepted,),
                name=_METRICS_THREAD_NAME,
                daemon=True,
            )
            # Published before the threads run, so `_retire`'s identity check
            # and the ticker's own can never fail to recognise which thread
            # they belong to.
            self._worker = worker
            self._metrics_stop.clear()
            self._metrics = ticker
            try:
                # Started under the same lock that published them, so no other
                # thread can see `_worker` before the thread is running. A
                # stop() landing here waits for start() to finish instead of
                # joining a thread that was never started -- a RuntimeError
                # out of stop(), and out of serve() as a traceback.
                # `Thread.start()` blocks only on its own `_started` event and
                # never re-enters the daemon, so holding the lock is safe.
                #
                # The ticker goes first because it is the half that can still
                # be taken back by hand: it publishes nothing until there is
                # speech, and setting an Event retires it. A speech worker
                # that has started cannot be withdrawn the same way -- it
                # retires itself, by identity, off the `_worker` below -- so
                # if it were up when the other half failed there would be
                # nothing honest left to roll back.
                ticker.start()
                worker.start()
            except BaseException:  # noqa: B036 - rolled back, then re-raised
                # "can't start new thread" would otherwise leave a daemon that
                # accepts speech with nothing to consume it and no way back:
                # with `_worker` set, every later start() quietly no-ops.
                if self._worker is worker:
                    self._worker = None
                    self._running = False
                if self._metrics is ticker:
                    # Signalled, not joined: the ticker takes this very lock on
                    # every pass, so joining it here would deadlock. It leaves
                    # at its next wake having published nothing, because the
                    # daemon it would report on is idle and shut.
                    self._metrics = None
                    self._metrics_stop.set()
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
            ticker = self._metrics
            # Retired here rather than after the worker's join, so that a
            # start() racing this cannot find the old ticker still installed
            # and leave two of them publishing. Both halves matter: the Event
            # wakes it now, and `_metrics` no longer being it is what stops a
            # ticker that woke on a timeout the next start() has since
            # cleared -- the same identity check `_retire` makes.
            self._metrics = None
            self._metrics_stop.set()
            # Set under the lock that guards it, as in the HUSH/CANCEL path
            # and for the same reason: released first, the worker could
            # finish this utterance and install the next job's Event in
            # between, leaving this set() on an Event nobody is watching
            # while that next job speaks on. `Event.set()` never blocks, so
            # holding the lock across it costs nothing.
            self._cancel.set()
        if ticker is not None:
            # Outside the lock it takes on every pass, and bounded like the
            # worker's join: a shutdown must not hang on a monitor. The only
            # thing that can hold it that long is a subscriber slow to take
            # the last event, and it is already retired by identity, so a
            # ticker that outlives this join publishes nothing after it.
            ticker.join(timeout=_JOIN_METRICS_SECONDS)
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

    def _stop_speaking(self, source_id: str, *, drain: bool, reason: str) -> int:
        """Stop what is sounding, and say how much queued speech went with it.

        Factored out of the HUSH/CANCEL branch so that mute and disable stop
        speech by this path rather than a second one: the ordering below is
        too particular to keep two copies of, and an off switch that lets the
        current paragraph finish is not an off switch.

        `source_id` empty means everything; a named one reaches only that
        channel, which is what a per-channel mute needs — silencing one
        session must not cut another off mid-sentence, nor throw away its
        queue. `_speaking` is what says whether the voice in the room is the
        one being silenced.

        The Event is read AND set under the lock. Set outside it, the worker
        could finish this utterance, take the next job off the queue and
        install its fresh Event in between — leaving this set() on an Event
        nobody is watching, the drain below finding an already-empty queue,
        and that next job speaking on after hush answered `ok`.
        `Event.set()` never blocks. Only `player.stop()` does, on a real
        device, which is why that one stays outside: holding the lock across
        it would stall enqueue. This narrows the window rather than closing
        it; `_speak` says what is left of it.
        """
        with self._idle:
            sounding = not source_id or self._speaking == source_id
            if sounding:
                self._cancel.set()
                if self._current is not None:
                    self._current.seek_to = None
        try:
            if sounding:
                self.player.stop()
        finally:
            # In a finally, because a sink that refuses to stop — the
            # headset out of range again — must not carry the drain off
            # with it. Without this, a hush that met a failing player
            # reported an error and left every queued utterance in place:
            # the daemon answering a request to stop talking by going on
            # talking. The failure still reaches the caller; only the
            # drain is no longer hostage to it.
            discarded = self._drain_queued(source_id, reason=reason) if drain else 0
        return discarded

    def _silence_for_mute(self, source_id: str) -> None:
        """Stop speech for a mute or a disable, reporting a failing sink rather than raising.

        Where this differs from hush: by the time it runs, the flag is
        already set and already persisted, so a raise here would report a
        failure for a switch that did in fact flip — and leave the caller
        unable to tell which. Hush has nothing behind it to be wrong about,
        which is why it lets the sink's failure out instead.
        """
        try:
            self._stop_speaking(source_id, drain=True, reason=_DISCARDED_MUTED)
        except Exception as exc:
            self._publish("error", source_id, {"message": f"could not silence the player: {exc!r}"})

    def _engine_status(self) -> dict[str, object]:
        """What STATUS says about the model, for an engine of either kind.

        An engine that cannot be unloaded reports as loaded, because it is: a
        control surface asking whether this daemon can speak must not read
        `false` off one that will speak perfectly well.
        """
        engine = self.engine
        if not isinstance(engine, Loadable):
            return {"loaded": True, "loading": False}
        return {"loaded": engine.loaded, "loading": engine.loading}

    def _set_engine_loaded(self, wanted: bool) -> Response:
        """Load or unload the model, without ever blocking the socket.

        Loading takes tens of seconds. Doing it on the request thread would
        stall every other client for the duration — including the hush of
        whoever changed their mind — so the verb returns at once and
        readiness is announced on the bus instead. Unloading is immediate and
        stays here.

        The flag is written before either happens, so that a daemon killed
        mid-load comes back to the state the user asked for rather than the
        one it managed to reach.
        """
        if not isinstance(self.engine, Loadable):
            return Response(
                ok=False,
                error=f"{type(self.engine).__name__} cannot be loaded or unloaded",
            )
        engine: Loadable = self.engine
        state.save(replace(state.load(), disabled=not wanted))
        if not wanted:
            # Silenced before the model goes: the utterance in flight is
            # holding audio synthesised from it, and letting that finish
            # would be the paragraph this switch exists to cut off.
            self._silence_for_mute("")
            engine.unload()
            self._publish("engine", "", {"state": "unloaded"})
            return Response(ok=True, data={"loaded": False})
        self._publish("engine", "", {"state": "loading"})

        def run() -> None:
            engine.load()
            self._publish("engine", "", {"state": "ready"})

        threading.Thread(target=run, name=_ENGINE_THREAD_NAME, daemon=True).start()
        return Response(ok=True, data={"loading": True})

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

    def _seek(self, payload: dict[str, object]) -> Response:
        """Move playback to another segment of the utterance being spoken.

        By stopping this pass and re-entering `speak()` at that segment. The
        audio of segments already made is in the utterance's cache, so going
        back is immediate; anything further ahead is synthesised as it would
        have been. Global, like pause: it moves whatever is being heard.

        Past the last segment is not an error. It is what pressing "next" on
        the last sentence means, and the answer is the one `cancel` gives:
        this utterance is over and the queue runs on.
        """
        given = [key for key in ("index", "by") if key in payload]
        value = payload.get(given[0]) if len(given) == 1 else None
        if not isinstance(value, int) or isinstance(value, bool):
            return Response(ok=False, error="seek needs one integer, 'index' or 'by'")
        with self._idle:
            current = self._current
            if current is None:
                return Response(ok=False, error="nothing is speaking")
            target = max(0, value if given[0] == "index" else current.index + value)
            ended = target >= len(current.units)
            current.seek_to = None if ended else target
            # Set under the lock for the reason `_stop_speaking` gives.
            self._cancel.set()
        self._stop_player()
        if ended:
            return Response(ok=True, data={"index": None, "ended": True})
        return Response(ok=True, data={"index": target})

    def _replay(self, index: object) -> Response:
        """Play the last utterance again, from sentence `index`.

        While that utterance is still being spoken this is a seek: "play from
        here" means the same thing whether or not the voice has reached the
        end yet, and a caller should not have to race the daemon to choose
        between two verbs. Once it has finished, it is queued again on the
        channel that first spoke it -- behind anything already waiting, and
        subject to the same off switches as new speech.

        Only what this daemon has already said can be replayed. That is why
        the window may send it at all: it cannot put words on another
        session's channel, only repeat ones that channel already spoke.
        """
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return Response(ok=False, error="replay needs a sentence 'index', from 0")
        with self._idle:
            last = self._last
            speaking = self._current is not None
        if last is None:
            return Response(ok=False, error="nothing to replay")
        if index >= len(last.units):
            return Response(
                ok=False, error=f"no sentence {index}: the last utterance has {len(last.units)}"
            )
        if speaking:
            return self._seek({"index": index})
        channel = self.channels.open(last.source_id)
        refusal = self._refusal(last.source_id, channel.muted, last.text, "replay")
        if refusal is not None:
            return refusal
        response = self._accept(
            _Job(
                source_id=last.source_id,
                text=last.text,
                profile=last.profile,
                prefix="",
                replay=last,
                start=index,
            )
        )
        if response.ok:
            response.data["index"] = index
        return response

    def _set_speed(self, raw: object) -> Response:
        """Change the listener's speed, answering with the one applied.

        Global, like pause: it is how fast *this person* wants to listen,
        not a property of any one channel. Clamped rather than refused when
        out of range, and the answer says so -- a client asking for 3 is told
        1.6 instead of being left to guess what it got. Kept across restarts
        like mute.
        """
        if (
            not isinstance(raw, (int, float))
            or isinstance(raw, bool)
            or not math.isfinite(raw)
            or raw <= 0
        ):
            return Response(ok=False, error="set_speed needs a positive number 'speed'")
        applied = self.tempo.set(float(raw))
        state.save(replace(state.load(), speed=applied))
        self._publish("speed", "", {"speed": applied})
        return Response(ok=True, data={"speed": applied})

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
            # Both are scoped by the source that sent them: empty stops the
            # whole daemon, a named one reaches that channel and no other.
            # That is a deliberate break — these verbs used to ignore their
            # source_id and stop everything, whatever was asked for — taken
            # because the callers were already written as though the narrow
            # meaning were the real one. `send.hush` has documented itself as
            # "stop `source` talking" the whole time, and the Claude Code hook
            # sends one hush per channel on every prompt: under the old
            # meaning the first of that pair silenced a second session that
            # had said nothing and was owed nothing. Mute reaches the same
            # helper with the same argument, so the two switches now narrow
            # the same way rather than one of them meaning something else.
            #
            # The scope is in the response, derived here from what the daemon
            # actually did rather than left for the caller to infer from the
            # request it sent. A client that assumed the two matched is what
            # this answer exists to catch.
            scope = "channel" if request.source_id else "all"
            discarded = self._stop_speaking(
                request.source_id, drain=request.verb is Verb.HUSH, reason=_DISCARDED_HUSHED
            )
            return Response(ok=True, data={"discarded": discarded, "scope": scope})
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
                            "muted": c.muted,
                        }
                        for c in self.channels.all()
                    ],
                    # What is waiting, as well as who is connected. A client
                    # learned of an utterance when it started and not before,
                    # so a queue thirty deep and an empty one read the same
                    # until the speech came out of it.
                    "queue": [
                        {"source_id": job.source_id, "text": _preview(job.text)}
                        for job in self._pending_jobs()
                    ],
                    # Both levels, separately. A GUI that had only the union
                    # could not draw the two switches it is being asked to
                    # draw: a channel that is audible behind a global mute
                    # looks identical to one that is muted itself.
                    "muted": self.muted,
                    "engine": self._engine_status(),
                    "speed": self.tempo.value,
                },
            )
        if request.verb in (Verb.PAUSE, Verb.RESUME):
            return self._set_paused(request.verb is Verb.PAUSE)
        if request.verb is Verb.SET_SPEED:
            return self._set_speed(request.payload.get("speed"))
        if request.verb is Verb.SEEK:
            return self._seek(request.payload)
        if request.verb is Verb.REPLAY:
            return self._replay(request.payload.get("index"))
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
        if request.verb is Verb.SET_LABEL:
            label = request.payload.get("label")
            # A blank label is what an unnamed channel already carries, so
            # storing one would answer `ok` to a request that leaves the
            # listing showing the source_id it was sent to replace.
            if not isinstance(label, str) or not label.strip():
                return Response(ok=False, error="set_label needs a non-empty label")
            self.channels.set_label(request.source_id, label)
            return Response(ok=True, data={"label": label})
        if request.verb is Verb.MUTE:
            wanted = request.payload.get("muted")
            if not isinstance(wanted, bool):
                return Response(ok=False, error="mute needs a boolean 'muted'")
            if request.source_id:
                self.channels.set_muted(request.source_id, wanted)
                scope = "channel"
            else:
                # Global, and kept apart from the per-channel flags rather
                # than written across them: clearing it has to give each
                # channel back what it had, not unmute everything. It names
                # no source, so it opens no channel, for the same reason
                # STATUS does not.
                self.muted = wanted
                state.save(replace(state.load(), muted=wanted))
                scope = "global"
            if wanted:
                # An off switch that lets the current paragraph finish is not
                # an off switch.
                self._silence_for_mute(request.source_id)
            self._publish("mute", request.source_id, {"muted": wanted, "scope": scope})
            return Response(ok=True, data={"muted": wanted, "scope": scope})
        if request.verb is Verb.SET_ENGINE:
            loaded = request.payload.get("loaded")
            if not isinstance(loaded, bool):
                return Response(ok=False, error="set_engine needs a boolean 'loaded'")
            return self._set_engine_loaded(loaded)
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
        refusal = self._refusal(request.source_id, channel.muted, text, kind)
        if refusal is not None:
            return refusal
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
        return self._accept(
            _Job(
                source_id=request.source_id,
                text=text,
                profile=profile,
                prefix=decision.prefix,
            )
        )

    def _refusal(
        self, source_id: str, channel_muted: bool, text: str, kind: str
    ) -> Response | None:
        """Why speech on this channel is dropped at the door, or None if it is not."""
        if self.muted or channel_muted:
            # Dropped at the door, not held: unmuting must not release ten
            # minutes of backlog into the room. Nothing reaches the worker,
            # so a muted channel costs no synthesis at all — which is the
            # difference between this and turning the volume down.
            self._publish("declined", source_id, {"text": text, "kind": kind, "reason": "muted"})
            return Response(ok=True, data={"spoken": False, "reason": "muted"})
        if isinstance(self.engine, Loadable) and not self.engine.loaded:
            # Disabled, or still loading: either way there is no model to say
            # this with. Dropped for the same reason as a mute, and it covers
            # the load as well as the disable deliberately — an enable that
            # queued thirty seconds of arrivals would speak them all at once
            # the moment the model landed.
            self._publish("declined", source_id, {"text": text, "kind": kind, "reason": "disabled"})
            return Response(ok=True, data={"spoken": False, "reason": "disabled"})
        return None

    def _accept(self, job: _Job) -> Response:
        """Put an accepted job on the queue and say so."""
        # Counted before the put, so the job is never in the queue while the
        # daemon still looks idle. Both under the lock stop() takes, so a
        # concurrent stop cannot slip its sentinel in between and leave this
        # job queued behind an ok response with no worker left to speak it.
        with self._idle:
            if not self._running:
                return Response(ok=False, error=_NOT_RUNNING)
            self._pending += 1
            self._accepted += 1
            self._jobs.put(job)
            # Read inside the same lock as the put, so the number announced is
            # the queue this job actually joined rather than one a concurrent
            # drain has since emptied.
            waiting = len(self._pending_jobs())
        # Announced only once the job is really on the queue, and outside the
        # lock: `EventBus.publish` runs its subscribers on this thread, and one
        # that turned round and asked the daemon anything would deadlock on a
        # lock that does not re-enter. A client watching the stream could not
        # otherwise see an utterance until it began speaking, which for a deep
        # queue is minutes after it was accepted.
        self._publish("queued", job.source_id, {"text": _preview(job.text), "pending": waiting})
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

    def _pending_jobs(self) -> list[_Job]:
        """What is waiting its turn, next first. Not what is being spoken.

        Read off `queue.Queue`'s own deque under `queue.Queue`'s own mutex.
        That deque is the very thing the Queue guards, so a snapshot taken
        under that lock cannot tear: no put or get can be half-done while this
        runs.

        A list of pending jobs kept alongside the queue would be a second
        source of truth for one fact, and the two would disagree the first
        time a path updated one and not the other -- `_drain_queued` puts
        filtered jobs back, `_empty_queue` takes everything, `stop()` deposits
        a sentinel. It is the argument `clients/claude_code/registry` makes
        for writing the file the hook already writes rather than adding a
        registration verb, and it holds harder here, where the two would be
        touched by four paths instead of one. There is no drift to fix
        because there is nothing to drift from.

        Taken after `_idle` wherever both are held, which is the order
        `_enqueue`'s own `put` already establishes. Nothing takes `_idle`
        while holding this one.

        The `None`s are stop signals for the worker, not speech anyone asked
        for, so they are not part of what is waiting to be said.
        """
        with self._jobs.mutex:
            items = list(self._jobs.queue)
        return [item for item in items if item is not None]

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

    def _drain_queued(self, source_id: str, *, reason: str) -> int:
        """Drop what is queued and report it, returning how many there were.

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

        `source_id` empty means everything. A named one keeps the other
        channels' jobs and puts them back in the order they came out, because
        muting one session must not throw away another's queue. Nothing can
        interleave with that: `_enqueue` puts under this same lock. The stop
        signals go back last, where `stop()` deposited them — behind the work
        they were meant to follow.

        `reason` is given rather than assumed for the same reason the two
        `_DISCARDED_` texts exist at all: a mute that reported its drops as a
        hush would send whoever read the event looking for a verb nobody
        sent.
        """
        with self._idle:
            taken, signals = self._empty_queue()
            jobs: list[_Job] = []
            for job in taken:
                if not source_id or job.source_id == source_id:
                    jobs.append(job)
                else:
                    self._jobs.put(job)
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
                        "reason": reason,
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
        # Installed with the Event and for the same reason: this is what the
        # metrics ticker reads drift off while the utterance is in flight, so
        # it must be in place before anything else can run. A fresh one per
        # utterance, since drift is measured across the current one.
        timeline = Timeline()
        with self._idle:
            self._cancel = cancel
            self._timeline = timeline
            # Installed with the Event it belongs to, so a channel-scoped
            # silence reading the two together can never match this job's
            # source against the previous job's Event.
            self._speaking = job.source_id
            # Read under the same lock that installs the Event, so a stop()
            # cannot land between the two and leave this job installed as the
            # current utterance and past its own liveness check at once.
            running = self._running
        if not running:
            # stop() can still land between this job leaving the queue and
            # the check above, so the Event alone cannot stop it.
            self._publish("error", job.source_id, {"message": _DISCARDED_STOPPED})
            return
        if job.replay is not None:
            # Said before, so already prepared: see `_Spoken` for why the
            # transforms are not run again.
            text = job.replay.text
            pieces = list(job.replay.pieces)
            units = list(job.replay.units)
        else:
            text = f"{job.prefix} {job.text}".strip() if job.prefix else job.text
            pieces, errors = job.profile.prepare([Piece(span=Span(0, len(text)), spoken=text)])
            for message in errors:
                self._publish("error", job.source_id, {"message": message})
            # Segmented here rather than inside `speak()`, so that `started`
            # can name every sentence before the first is heard. A monitor
            # showing the whole utterance needs them all up front, and a seek
            # addresses them by the index given here.
            units = segmenter.segment(pieces)
        with self._idle:
            self._last = _Spoken(
                source_id=job.source_id,
                profile=job.profile,
                text=text,
                pieces=tuple(pieces),
                units=tuple(units),
            )
        self._publish(
            "started",
            job.source_id,
            {
                "text": text,
                "segments": [
                    {
                        "index": i,
                        "text": unit.spoken,
                        "span_start": unit.span.start,
                        "span_end": unit.span.end,
                    }
                    for i, unit in enumerate(units)
                ],
            },
        )
        if cancel.is_set():
            # Hushed during `prepare` or by a subscriber of `started` itself.
            # `speak()` would notice this too, but only after synthesising and
            # playing the first segment.
            self._publish("finished", job.source_id, {"cancelled": True, "aborted": False})
            return
        # Installed only now, past the early return above, which would
        # otherwise leave it pointing at an utterance that never spoke --
        # and seek and replay treating that as the one in the room.
        current = _Current(units=tuple(units))
        with self._idle:
            self._current = current

        def announce(segment: Segment) -> None:
            """Publish one segment's position, as that segment starts playing.

            Called by `speak()` from a thread of its own, so this may take as
            long as the bus does without holding up a word of speech. What it
            replaced read `result.timeline.segments` after `speak()` had
            returned, which meant every position of an utterance arrived in a
            single burst once the speaking was over -- measured against the
            running daemon as five positions and `finished` at the same
            instant, eight seconds after `started`. A monitor highlighting
            the sentence being spoken could do nothing with any of them. The
            `played_at` inside each was right the whole time; only its
            delivery was late, which is why nothing short of watching arrival
            times could show it.
            """
            with self._idle:
                current.index = segment.index
            self._publish(
                "position",
                job.source_id,
                {
                    "text": segment.text,
                    "span_start": segment.span.start,
                    "span_end": segment.span.end,
                    "audio_offset": segment.audio_offset,
                    "played_at": segment.played_at,
                    "index": segment.index,
                },
            )

        cache = AudioCache()
        start = min(job.start, max(0, len(units) - 1))
        try:
            while True:
                try:
                    result = speak(
                        pieces,
                        self.engine,
                        self.player,
                        voice=job.profile.voice,
                        speed=job.profile.speed,
                        cancel=cancel,
                        window=self._synthesis,
                        timeline=timeline,
                        on_playing=announce,
                        units=units,
                        cache=cache,
                        tempo=self.tempo,
                        start_index=start,
                    )
                except BaseException as exc:  # noqa: B036 - re-raising would drop `finished`
                    # `started` is already out. A subscriber pairing the two
                    # would wait for a `finished` that never came, so send
                    # both — including when the engine or the player raises
                    # outside `Exception`. `!r` because `str(SystemExit(3))`
                    # is just "3": a diagnostic that names neither the
                    # exception nor its type is no diagnostic.
                    self._publish("error", job.source_id, {"message": f"synthesis failed: {exc!r}"})
                    self._publish("finished", job.source_id, {"cancelled": False, "aborted": True})
                    return
                # No position loop here any more: they went out as they were
                # spoken. `speak()` has already waited for its own reporting
                # thread to drain, so every position of this pass is on the
                # bus ahead of whatever comes next.
                for message in result.errors:
                    self._publish("error", job.source_id, {"message": message})
                with self._idle:
                    target, current.seek_to = current.seek_to, None
                    if target is None or not self._running:
                        break
                    # A seek: the same utterance again from `target`, with a
                    # fresh Event and timeline, and no `finished`/`started`
                    # pair around it -- the next `position` is the whole of
                    # what a monitor needs to be told.
                    cancel = threading.Event()
                    timeline = Timeline()
                    self._cancel = cancel
                    self._timeline = timeline
                clear = getattr(self.player, "clear_interrupt", None)
                if callable(clear):
                    clear()
                start = target
        finally:
            with self._idle:
                if self._current is current:
                    self._current = None
        self._publish(
            "finished",
            job.source_id,
            {"cancelled": result.cancelled, "aborted": result.aborted},
        )

    def _tick_metrics(self, settled: int) -> None:
        """Publish `metrics` once an interval while there is speech to report.

        On its own thread, which is the whole point. Every number in the event
        is read from state the speech worker has already written down -- the
        pending count, the window the pipeline appends to as it synthesises,
        the timeline it builds as it plays -- so the worker never waits on a
        measurement, and a subscriber slow to take one of these stalls the
        ticker rather than the speech.

        Nothing goes out while the daemon is idle: a monitor that has been
        told nothing since the last `finished` knows its numbers are stale in
        the only way that matters. The exception is the tick that finds the
        daemon newly idle, which goes out so the display settles at zero
        instead of freezing on the last busy value.

        `settled` is the accepted-work count this ticker starts from, taken by
        `start()` under its own lock. Work accepted after that is work this
        has not yet reported on, which is what makes an utterance that began
        and ended between two ticks still settle the display.
        """
        me = threading.current_thread()
        while not self._metrics_stop.wait(self._metrics_interval):
            with self._idle:
                if self._metrics is not me:
                    # Retired by a stop(), which a start() may already have
                    # followed: the Event this woke on belongs to the ticker
                    # that came after. Identity says which of us is current,
                    # as it does for the speech worker in `_retire`.
                    return
                pending = self._pending
                accepted = self._accepted
                timeline = self._timeline
            if pending == 0:
                if accepted == settled:
                    continue
                # Idle, with work finished since the last time this said so.
                # One event to settle the display, then silence.
                settled = accepted
            # Gathered outside the lock: reading /proc and averaging the
            # window have nothing to do with the queue, and holding the lock
            # across them would put an enqueue behind a file read.
            self._publish("metrics", "", self._metrics_payload(pending, timeline))

    def _metrics_payload(self, pending: int, timeline: Timeline | None) -> dict[str, object]:
        """The four numbers, leaving out any that is not knowable here.

        `rtf` is missing only before the first segment of the daemon's life
        has been synthesised, and `mem_bytes` only where there is no `/proc`.
        Both are left out rather than sent as zero: a monitor can show a dash
        for a number it was not given, where a zero it was given is a reading.
        """
        data: dict[str, object] = {}
        rtf = self._synthesis.rtf()
        if rtf is not None:
            data["rtf"] = rtf
        data["drift"] = timeline.drift if timeline is not None else 0.0
        resident = resident_bytes()
        if resident is not None:
            data["mem_bytes"] = resident
        data["queue"] = pending
        return data

    def _publish(self, kind: str, source_id: str, data: dict[str, object]) -> None:
        self.bus.publish(Event(kind=kind, source_id=source_id, data=data))
