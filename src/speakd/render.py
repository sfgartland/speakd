"""Renders long text to audio files in the background, off live speech.

A render is a queue of `RenderJob`s worked one at a time by a dedicated
thread. It never shares the live pipeline's queue -- a render must never wait
behind an agent's utterance -- which the next change makes true the other
direction too, by teaching the queue to yield to live speech in progress.

Each job's progress survives a crash: a work directory holds a manifest
(the parts, and the next part and sentence to synthesise) and one WAV per
part, appended to as each sentence finishes and never rewritten from
scratch. A daemon that restarts mid-render re-queues the job and picks up
exactly where the manifest says, without re-synthesising anything already on
disk.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from speakd.model import Piece, Span
from speakd.paths import client_state_dir
from speakd.segmenter import segment
from speakd.synth import Synthesizer

# The states a job passes through. Terminal ones are never left once entered.
# `paused` does not appear yet -- it belongs to yielding, added next.
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

DEFAULT_SENTENCE_GAP_MS = 250
DEFAULT_CHAPTER_GAP_MS = 1500
DEFAULT_BITRATE = "64k"


@dataclass
class Part:
    """One chapter (or the whole thing, for an article): a title and text."""

    title: str
    text: str


@dataclass
class RenderJob:
    """One render, and everything needed to resume it after a restart.

    Every field here is exactly what the manifest holds -- there is no
    separate "manifest schema", so a round trip through `to_manifest` and
    `from_manifest` can never drift from what the job actually is.

    `chapter_gap_ms` and `bitrate` are carried from the start even though
    nothing reads them yet: they belong to encoding, which does not exist
    until a later change, but the manifest format is not something a
    resumed job can afford to have change shape out from under it.
    """

    id: str
    parts: list[Part]
    out: Path
    format: str  # "mp3" | "opus" | "m4b"
    lang: str
    voice: str
    speed: float = 1.0
    metadata: dict[str, str] = field(default_factory=dict)
    sentence_gap_ms: int = DEFAULT_SENTENCE_GAP_MS
    chapter_gap_ms: int = DEFAULT_CHAPTER_GAP_MS
    bitrate: str = DEFAULT_BITRATE
    state: str = "queued"
    # Where to resume: the next part to synthesise, and the next sentence
    # within it. Parts before `part_index` are complete and never revisited.
    part_index: int = 0
    sentence_index: int = 0
    done_seconds: float = 0.0
    estimate_seconds: float = 0.0
    error: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["parts"] = [{"title": p.title, "text": p.text} for p in self.parts]
        data["out"] = str(self.out)
        return data

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> RenderJob:
        kwargs = dict(data)
        kwargs["parts"] = [Part(**p) for p in data["parts"]]
        kwargs["out"] = Path(data["out"])
        return cls(**kwargs)

    def state_dict(self) -> dict[str, Any]:
        """What `status` and the `render` event will publish."""
        return {
            "job": self.id,
            "state": self.state,
            "part": self.part_index,
            "parts": len(self.parts),
            "done_seconds": self.done_seconds,
            "estimate_seconds": self.estimate_seconds,
            "out": str(self.out) if self.state == "done" else None,
            "error": self.error,
        }


def new_job_id() -> str:
    return uuid.uuid4().hex


# --- the manifest on disk -------------------------------------------------


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / "manifest.json"


def save_manifest(job: RenderJob, work_dir: Path) -> None:
    """Write the manifest atomically, so a reader never sees a half-written one."""
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(job.to_manifest(), indent=2), encoding="utf-8")
    os.replace(tmp, _manifest_path(work_dir))


def load_manifest(work_dir: Path) -> RenderJob:
    data = json.loads(_manifest_path(work_dir).read_text(encoding="utf-8"))
    return RenderJob.from_manifest(data)


# --- WAV parts, appended to one sentence at a time ------------------------
#
# The stdlib `wave` module has no append mode, so it is not used here: every
# append re-patches a 44-byte canonical header by hand instead. That header
# is rewritten after every append, so the file on disk is always a valid
# WAV of exactly the audio actually written -- a crash mid-part leaves a
# shorter, still-playable file, never a corrupt one with a header that lies
# about how much data follows.

_HEADER_SIZE = 44


def _wav_header(data_bytes: int, sample_rate: int) -> bytes:
    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


def _wav_info(path: Path) -> tuple[int, int]:
    """(sample_rate, data_bytes), read from the header written above."""
    with path.open("rb") as fh:
        header = fh.read(_HEADER_SIZE)
    sample_rate = struct.unpack("<I", header[24:28])[0]
    data_bytes = struct.unpack("<I", header[40:44])[0]
    return sample_rate, data_bytes


def wav_duration(path: Path) -> float:
    """Seconds of audio in a WAV written by `append_pcm`."""
    sample_rate, data_bytes = _wav_info(path)
    return data_bytes / (sample_rate * 2)


def append_pcm(path: Path, pcm: bytes, sample_rate: int) -> None:
    """Append 16-bit mono PCM to `path`, creating it with a fresh header if new."""
    if not path.exists():
        path.write_bytes(_wav_header(0, sample_rate))
    _, existing = _wav_info(path)
    with path.open("r+b") as fh:
        fh.seek(0, os.SEEK_END)
        fh.write(pcm)
        fh.seek(0)
        fh.write(_wav_header(existing + len(pcm), sample_rate))


def _pcm16(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm: bytes = (clipped * 32767.0).astype(np.int16).tobytes()
    return pcm


def _sentences(text: str) -> list[str]:
    """Segment one part's text exactly as the live pipeline segments an utterance."""
    if not text.strip():
        return []
    piece = Piece(span=Span(0, len(text)), spoken=text)
    return [p.spoken for p in segment([piece])]


# --- cancellation ------------------------------------------------------


class _Cancelled(Exception):
    """Internal signal: unwind `_render` without touching `out`."""


# --- the queue ---------------------------------------------------------


class RenderQueue:
    """Runs render jobs on one worker thread, in submission order.

    Nothing here yet knows about live speech -- that arrives with yielding,
    next -- so for now a render simply runs each part's sentences through
    the engine back to back, exactly like a very long utterance.
    """

    def __init__(
        self,
        engine: Synthesizer,
        work_root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._work_root = work_root or client_state_dir("renders")
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, RenderJob] = {}
        self._queue: list[str] = []
        self._cancelled: set[str] = set()
        self._wake = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _work_dir(self, job_id: str) -> Path:
        return self._work_root / job_id

    def submit(self, job: RenderJob) -> str:
        save_manifest(job, self._work_dir(job.id))
        with self._lock:
            self._jobs[job.id] = job
            self._queue.append(job.id)
        self._wake.set()
        return job.id

    def resume(self) -> None:
        """Re-queue every unfinished manifest under the work root.

        Called once at daemon start. A manifest already `done`, `failed` or
        `cancelled` is left alone -- resuming is only for a render that was
        still going when the process that owned it stopped existing.
        """
        if not self._work_root.exists():
            return
        for entry in sorted(self._work_root.iterdir()):
            if not entry.is_dir() or not (entry / "manifest.json").exists():
                continue
            job = load_manifest(entry)
            if job.state in _TERMINAL_STATES:
                continue
            job.state = "queued"
            save_manifest(job, entry)
            with self._lock:
                self._jobs[job.id] = job
                self._queue.append(job.id)
        self._wake.set()

    def cancel(self, job_id: str) -> bool:
        """Stop a job and remove its work directory.

        A queued job is simply dropped, without disturbing whatever is
        currently running. A running one is flagged, and `_render` unwinds
        and cleans up on its own the next time it checks -- never from this
        thread, which does not own the job's files.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job_id in self._queue:
                self._queue.remove(job_id)
                job.state = "cancelled"
                shutil.rmtree(self._work_dir(job_id), ignore_errors=True)
                return True
            if job.state == "running":
                self._cancelled.add(job_id)
                return True
            return False

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.state_dict() for job in self._jobs.values()]

    def job(self, job_id: str) -> RenderJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, timeout: float = 5.0) -> None:
        """For tests: stop the worker thread after its current job settles."""
        self._stopping = True
        self._wake.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stopping:
            job_id = None
            with self._lock:
                if self._queue:
                    job_id = self._queue.pop(0)
            if job_id is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._render(self._jobs[job_id])

    def _render(self, job: RenderJob) -> None:
        work_dir = self._work_dir(job.id)
        job.state = "running"
        try:
            self._synthesize(job, work_dir)
        except _Cancelled:
            job.state = "cancelled"
            self._cancelled.discard(job.id)
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        job.state = "done"
        shutil.rmtree(work_dir, ignore_errors=True)

    def _synthesize(self, job: RenderJob, work_dir: Path) -> None:
        for part_i in range(job.part_index, len(job.parts)):
            part = job.parts[part_i]
            sentences = _sentences(part.text)
            start = job.sentence_index if part_i == job.part_index else 0
            wav_path = work_dir / f"part-{part_i}.wav"
            for sent_i in range(start, len(sentences)):
                if job.id in self._cancelled:
                    raise _Cancelled
                text = sentences[sent_i]
                audio = self._engine.synthesize(text, job.voice, job.speed)
                append_pcm(wav_path, _pcm16(audio), self._engine.sample_rate)
                job.done_seconds += len(audio) / self._engine.sample_rate
                is_last_sentence = sent_i == len(sentences) - 1
                if not is_last_sentence and job.sentence_gap_ms:
                    gap_frames = int(self._engine.sample_rate * job.sentence_gap_ms / 1000)
                    append_pcm(wav_path, b"\x00" * (gap_frames * 2), self._engine.sample_rate)
                job.sentence_index = sent_i + 1
                save_manifest(job, work_dir)
            job.part_index = part_i + 1
            job.sentence_index = 0
            save_manifest(job, work_dir)
