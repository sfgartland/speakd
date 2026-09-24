"""Tests for background rendering: the queue, the manifest, and resume."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from speakd.render import (
    Part,
    RenderJob,
    RenderQueue,
    append_pcm,
    load_manifest,
    new_job_id,
    save_manifest,
    wav_duration,
)
from speakd.synth.fake import FakeEngine


class CountingEngine(FakeEngine):
    """Records every text handed to `synthesize`, to prove resume skips work."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.calls.append(text)
        return super().synthesize(text, voice, speed)


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
    job = _job(tmp_path, part_index=1, sentence_index=2, done_seconds=3.5, state="running")
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


def test_append_pcm_creates_then_appends(tmp_path: Path) -> None:
    path = tmp_path / "part-0.wav"
    append_pcm(path, b"\x00\x01" * 100, sample_rate=1000)
    first_duration = wav_duration(path)
    append_pcm(path, b"\x00\x01" * 100, sample_rate=1000)
    second_duration = wav_duration(path)

    assert second_duration == pytest.approx(2 * first_duration)
    assert first_duration == pytest.approx(100 / 1000)


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
    append_pcm(work_dir / "part-0.wav", b"\x00\x00" * 10, sample_rate=engine_rate)

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
