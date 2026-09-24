"""Tests for A3: ffmpeg encoding and chapters.

Real ffmpeg tests are skipped when it is not on `PATH` (CI installs it via
apt; a local checkout without it just skips rather than failing). The
failure-path tests never touch the real binary -- they put a fake `ffmpeg`
script first on `PATH` so the outcome is deterministic and fast.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from speakd.render import Part, RenderJob, RenderQueue, ffmpeg_available, new_job_id
from speakd.synth.fake import FakeEngine

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _job(tmp_path: Path, **overrides: object) -> RenderJob:
    defaults: dict[str, Any] = dict(
        id=new_job_id(),
        parts=[Part(title="Whole", text="One. Two. Three. Four.")],
        out=tmp_path / "out.mp3",
        format="mp3",
        lang="en",
        voice="af_heart",
        speed=1.0,
        metadata={"title": "T", "artist": "A", "album": "B", "date": "2026"},
    )
    defaults.update(overrides)
    return RenderJob(**defaults)


def _run_to_terminal(queue: RenderQueue, job_id: str, timeout: float = 10.0) -> str:
    assert until(
        lambda: queue.job(job_id) is not None and queue.job(job_id).state in ("done", "failed"),  # type: ignore[union-attr]
        timeout=timeout,
    )
    job = queue.job(job_id)
    assert job is not None
    return job.state


def _ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_chapters",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data: dict[str, Any] = json.loads(result.stdout)
    return data


# --- real ffmpeg: mp3 -------------------------------------------------


@needs_ffmpeg
def test_mp3_render_has_expected_duration_and_tags(tmp_path: Path) -> None:
    engine = FakeEngine()  # 15 chars/s, sentence_gap default 250ms
    queue = RenderQueue(engine, work_root=tmp_path / "renders")
    job = _job(tmp_path)
    queue.submit(job)

    assert _run_to_terminal(queue, job.id) == "done"
    queue.stop()

    assert job.out.exists()
    probe = _ffprobe(job.out)
    duration = float(probe["format"]["duration"])
    # "One." "Two." "Three." "Four." at 15 chars/s, plus three 250ms sentence
    # gaps: (4+4+6+5)/15 + 3*0.25 = 2.0167s.
    expected = (4 + 4 + 6 + 5) / 15.0 + 3 * 0.25
    assert duration == pytest.approx(expected, rel=0.05)

    tags = probe["format"]["tags"]
    assert tags["title"] == "T"
    assert tags["artist"] == "A"
    assert tags["album"] == "B"
    assert tags["date"] == "2026"


# --- real ffmpeg: m4b with chapters ------------------------------------


@needs_ffmpeg
def test_m4b_render_has_chapters_and_tags(tmp_path: Path) -> None:
    engine = FakeEngine()
    queue = RenderQueue(engine, work_root=tmp_path / "renders")
    job = _job(
        tmp_path,
        out=tmp_path / "out.m4b",
        format="m4b",
        parts=[
            Part(title="Chapter One", text="One. Two."),
            Part(title="Chapter Two", text="Three. Four."),
        ],
    )
    queue.submit(job)

    assert _run_to_terminal(queue, job.id) == "done"
    queue.stop()

    assert job.out.exists()
    probe = _ffprobe(job.out)

    # Total duration: part 0 (0.2667 + 0.25 + 0.2667), the 1.5s chapter gap,
    # part 1 (0.4 + 0.25 + 0.3333).
    expected_total = (4 / 15 + 0.25 + 4 / 15) + 1.5 + (6 / 15 + 0.25 + 5 / 15)
    assert float(probe["format"]["duration"]) == pytest.approx(expected_total, rel=0.05)

    chapters = probe["chapters"]
    assert len(chapters) == 2
    assert chapters[0]["tags"]["title"] == "Chapter One"
    assert chapters[1]["tags"]["title"] == "Chapter Two"
    assert float(chapters[0]["start_time"]) == pytest.approx(0.0, abs=0.05)
    # Chapter 1 starts after part 0's real duration plus the chapter gap --
    # an mp4 chapter track carries only start times, so this is the one
    # figure the container actually preserves for us to check.
    expected_chapter_1_start = (4 / 15 + 0.25 + 4 / 15) + 1.5
    assert float(chapters[1]["start_time"]) == pytest.approx(expected_chapter_1_start, rel=0.05)

    tags = probe["format"]["tags"]
    assert tags["title"] == "T"
    assert tags["artist"] == "A"
    assert tags["album"] == "B"
    assert tags["date"] == "2026"


# --- real ffmpeg: opus smoke test ---------------------------------------


@needs_ffmpeg
def test_opus_render_smoke(tmp_path: Path) -> None:
    engine = FakeEngine()
    queue = RenderQueue(engine, work_root=tmp_path / "renders")
    job = _job(tmp_path, out=tmp_path / "out.opus", format="opus")
    queue.submit(job)

    assert _run_to_terminal(queue, job.id) == "done"
    queue.stop()

    assert job.out.exists()
    probe = _ffprobe(job.out)
    expected = (4 + 4 + 6 + 5) / 15.0 + 3 * 0.25
    assert float(probe["format"]["duration"]) == pytest.approx(expected, rel=0.05)
    # Ogg Opus carries vorbis comments on the stream, not the format, block.
    tags = probe["streams"][0]["tags"]
    assert tags["title"] == "T"
    assert tags["artist"] == "A"


# --- the failure path: a fake ffmpeg on PATH ----------------------------


def test_ffmpeg_failure_leaves_job_failed_with_stderr_tail_and_keeps_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "ffmpeg"
    script.write_text("#!/bin/sh\necho 'boom: disk full' 1>&2\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert ffmpeg_available()  # the fake is still "found"

    engine = FakeEngine()
    work_root = tmp_path / "renders"
    queue = RenderQueue(engine, work_root=work_root)
    job = _job(tmp_path, out=tmp_path / "out.mp3")
    queue.submit(job)

    assert _run_to_terminal(queue, job.id) == "failed"
    queue.stop()

    assert job.error is not None
    assert "boom: disk full" in job.error
    assert (work_root / job.id).exists()  # kept for a retry
    assert not job.out.exists()
    assert not job.out.with_name(job.out.name + ".partial").exists()


def test_no_ffmpeg_on_path_fails_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert not ffmpeg_available()

    engine = FakeEngine()
    work_root = tmp_path / "renders"
    queue = RenderQueue(engine, work_root=work_root)
    job = _job(tmp_path, out=tmp_path / "out.mp3")
    queue.submit(job)

    assert _run_to_terminal(queue, job.id) == "failed"
    queue.stop()

    assert job.error is not None
    assert "ffmpeg" in job.error.lower()
    assert not job.out.exists()
