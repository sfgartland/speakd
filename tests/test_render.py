"""Tests for background rendering: the queue, the manifest, and resume."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.render import (
    Part,
    RenderJob,
    RenderQueue,
    append_pcm,
    load_manifest,
    new_job_id,
    pcm_duration,
    save_manifest,
)
from speakd.synth.fake import FakeEngine


class CountingEngine(FakeEngine):
    """Records every text handed to `synthesize`, to prove resume skips work."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.calls.append(text)
        return super().synthesize(text, voice, speed, lang)


def until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def state_of(queue: RenderQueue, job_id: str) -> str:
    job = queue.job(job_id)
    assert job is not None
    return job.state


def _job(tmp_path: Path, **overrides: object) -> RenderJob:
    defaults: dict[str, Any] = dict(
        id=new_job_id(),
        parts=[Part(title="Chapter", text="One. Two. Three. Four.")],
        out=tmp_path / "out.mp3",
        format="mp3",
        lang="en",
        voice="af_heart",
        speed=1.0,
        metadata={"title": "T", "artist": "A", "album": "B", "date": "2026"},
    )
    defaults.update(overrides)
    return RenderJob(**defaults)


def test_manifest_round_trip(tmp_path: Path) -> None:
    job = _job(
        tmp_path,
        part_index=1,
        sentence_index=2,
        done_seconds=3.5,
        state="running",
        sample_rate=24000,
    )
    work_dir = tmp_path / "work"
    save_manifest(job, work_dir)

    assert json.loads((work_dir / "manifest.json").read_text())["state"] == "running"

    loaded = load_manifest(work_dir)
    assert loaded.id == job.id
    assert loaded.parts == job.parts
    assert loaded.out == job.out
    assert loaded.format == "mp3"
    assert loaded.part_index == 1
    assert loaded.sentence_index == 2
    assert loaded.done_seconds == 3.5
    assert loaded.metadata == job.metadata
    assert loaded.sample_rate == 24000


def test_append_pcm_creates_then_appends(tmp_path: Path) -> None:
    path = tmp_path / "part-0.pcm"
    append_pcm(path, b"\x00\x01" * 100)
    first_duration = pcm_duration(path, sample_rate=1000)
    append_pcm(path, b"\x00\x01" * 100)
    second_duration = pcm_duration(path, sample_rate=1000)

    assert second_duration == pytest.approx(2 * first_duration)
    assert first_duration == pytest.approx(100 / 1000)


def test_pcm_duration_of_a_missing_file_is_zero(tmp_path: Path) -> None:
    assert pcm_duration(tmp_path / "nope.pcm", sample_rate=1000) == 0.0


def test_render_runs_to_done_and_synthesizes_every_sentence(tmp_path: Path) -> None:
    engine = CountingEngine()
    queue = RenderQueue(engine, work_root=tmp_path / "renders")
    job = _job(tmp_path)
    queue.submit(job)

    assert until(lambda: state_of(queue, job.id) in ("done", "failed"))
    assert state_of(queue, job.id) == "done"
    assert engine.calls == ["One.", "Two.", "Three.", "Four."]
    queue.stop()


def test_resume_continues_at_the_recorded_sentence_without_resynthesizing(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "renders"
    engine_rate = 24000
    job = _job(tmp_path, part_index=0, sentence_index=2, state="running")
    work_dir = work_root / job.id
    save_manifest(job, work_dir)
    # Sentences 0 and 1 ("One.", "Two.") are already on disk, exactly as a
    # real crash would leave them -- resume must not touch them again.
    append_pcm(work_dir / "part-0.pcm", b"\x00\x00" * 10)

    engine = CountingEngine(sample_rate=engine_rate)
    queue = RenderQueue(engine, work_root=work_root)
    queue.resume()

    assert until(
        lambda: queue.job(job.id) is not None and state_of(queue, job.id) in ("done", "failed")
    )
    assert state_of(queue, job.id) == "done"
    # Only the two unfinished sentences were ever handed to the engine.
    assert engine.calls == ["Three.", "Four."]
    queue.stop()


def test_resume_ignores_finished_manifests(tmp_path: Path) -> None:
    work_root = tmp_path / "renders"
    job = _job(tmp_path, state="done")
    save_manifest(job, work_root / job.id)

    engine = CountingEngine()
    queue = RenderQueue(engine, work_root=work_root)
    queue.resume()
    time.sleep(0.2)

    assert queue.job(job.id) is None
    assert engine.calls == []
    queue.stop()


def test_cancel_queued_job_does_not_touch_the_running_one(tmp_path: Path) -> None:
    running_engine = FakeEngine(synthesis_cost=0.2)
    queue = RenderQueue(running_engine, work_root=tmp_path / "renders")
    running = _job(tmp_path, out=tmp_path / "running.mp3")
    queued = _job(tmp_path, out=tmp_path / "queued.mp3")
    queue.submit(running)
    queue.submit(queued)

    assert until(lambda: state_of(queue, running.id) == "running")
    assert queue.cancel(queued.id) is True

    assert state_of(queue, queued.id) == "cancelled"
    assert not (tmp_path / "renders" / queued.id).exists()
    assert state_of(queue, running.id) == "running"

    assert until(lambda: state_of(queue, running.id) in ("done", "failed"))
    assert state_of(queue, running.id) == "done"
    queue.stop()


def test_cancel_running_job_stops_it_and_removes_its_work_dir(tmp_path: Path) -> None:
    engine = FakeEngine(synthesis_cost=0.05)
    queue = RenderQueue(engine, work_root=tmp_path / "renders")
    job = _job(tmp_path, parts=[Part(title="C", text="One. Two. Three. Four. Five. Six.")])
    queue.submit(job)

    assert until(lambda: state_of(queue, job.id) == "running")
    assert queue.cancel(job.id) is True

    assert until(lambda: state_of(queue, job.id) == "cancelled")
    assert not (tmp_path / "renders" / job.id).exists()
    assert not job.out.exists()
    queue.stop()


# --- yielding to live speech -----------------------------------------------


def test_render_waits_while_busy_and_resumes_once_free(tmp_path: Path) -> None:
    engine = CountingEngine()
    busy_flag = {"value": True}
    queue = RenderQueue(
        engine,
        threading.Lock(),
        busy=lambda: busy_flag["value"],
        work_root=tmp_path / "renders",
        poll_seconds=0.01,
    )
    job = _job(tmp_path)
    queue.submit(job)

    assert until(lambda: state_of(queue, job.id) == "paused")
    time.sleep(0.1)
    # Nothing was synthesised while busy: yielding blocks *before* the
    # sentence, not partway through it.
    assert engine.calls == []

    busy_flag["value"] = False
    assert until(lambda: state_of(queue, job.id) in ("done", "failed"))
    assert state_of(queue, job.id) == "done"
    assert engine.calls == ["One.", "Two.", "Three.", "Four."]
    queue.stop()


def test_synth_lock_is_held_only_around_synthesize(tmp_path: Path) -> None:
    lock = threading.Lock()
    observed: list[bool] = []

    class LockCheckingEngine(FakeEngine):
        def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
            observed.append(lock.locked())
            return super().synthesize(text, voice, speed, lang)

    queue = RenderQueue(
        LockCheckingEngine(), lock, busy=lambda: False, work_root=tmp_path / "renders"
    )
    job = _job(tmp_path)
    queue.submit(job)

    assert until(lambda: state_of(queue, job.id) in ("done", "failed"))
    assert state_of(queue, job.id) == "done"
    # Every call happened with the lock held...
    assert observed == [True, True, True, True]
    # ...and it was released again afterwards, for the live pipeline to take.
    assert not lock.locked()
    queue.stop()


def test_a_live_utterance_is_never_delayed_by_more_than_one_sentence(tmp_path: Path) -> None:
    """Models a slow live utterance the way `tests/test_seek.py`'s `SlowSink`
    models slow playback: `busy()` flips true mid-render, and the render must
    reach `paused` almost immediately rather than after several more
    sentences.
    """
    lock = threading.Lock()
    engine = FakeEngine(synthesis_cost=0.05)
    busy_flag = {"value": False}
    queue = RenderQueue(
        engine,
        lock,
        busy=lambda: busy_flag["value"],
        work_root=tmp_path / "renders",
        poll_seconds=0.01,
    )
    job = _job(tmp_path, parts=[Part(title="C", text="One. Two. Three. Four. Five. Six.")])
    queue.submit(job)

    assert until(lambda: state_of(queue, job.id) == "running")
    busy_flag["value"] = True
    started = time.monotonic()
    assert until(lambda: state_of(queue, job.id) == "paused", timeout=1.0)
    # The pause landed well within one sentence's synthesis time of the
    # request, not after several more sentences.
    assert time.monotonic() - started < 0.3

    busy_flag["value"] = False
    assert until(lambda: state_of(queue, job.id) in ("done", "failed"))
    assert state_of(queue, job.id) == "done"
    queue.stop()


# --- a real live utterance, through a real daemon ---------------------------


class SlowSink(FakeSink):
    """Takes real time per block, as in `tests/test_seek.py`, so the live
    utterance is still in flight while the render has to yield to it."""

    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000 / 20)  # twenty times real time


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


def test_a_live_enqueue_during_a_render_starts_within_one_sentence(tmp_path: Path) -> None:
    """The review focus, end to end: one engine, one lock, a real daemon.

    A render is running when a live enqueue arrives. The live utterance must
    start within one sentence's synthesis time -- it may wait on the render
    sentence in flight, and on nothing more -- and the render must then yield
    for as long as the live utterance plays.
    """
    engine = FakeEngine(synthesis_cost=0.05)
    d = Daemon(
        engine,
        StreamingPlayer(SlowSink(), chunk_frames=512),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    d.start()
    queue = RenderQueue(
        engine,
        d.synth_lock,
        busy=lambda: d.speaking,
        work_root=tmp_path / "renders",
        poll_seconds=0.01,
    )
    try:
        long_text = " ".join(f"Sentence{i}." for i in range(12))
        job = _job(tmp_path, parts=[Part(title="C", text=long_text)])
        queue.submit(job)
        assert until(lambda: state_of(queue, job.id) == "running")

        enqueued = time.monotonic()
        response = d.handle(
            Request(
                verb=Verb.ENQUEUE,
                source_id="live",
                payload={"text": "Zero. One. Two. Three.", "kind": "response"},
            )
        )
        assert response.ok
        assert until(lambda: any(e.kind == "position" for e in list(seen)))
        # One sentence costs 0.05 s here. The live utterance waited on at
        # most the render sentence already in flight -- never on several,
        # and never on the render as a whole.
        assert time.monotonic() - enqueued < 0.3

        # While the live utterance plays, the render yields...
        assert until(lambda: state_of(queue, job.id) == "paused")
        assert d.wait_idle(timeout=10.0)
        # ...and runs on once it is over.
        assert until(lambda: state_of(queue, job.id) in ("done", "failed"))
        assert state_of(queue, job.id) == "done"
    finally:
        queue.stop()
        d.stop()
