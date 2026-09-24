"""Renders long text to audio files in the background, off live speech.

A render is a queue of `RenderJob`s worked one at a time by a dedicated
thread. It never shares the live pipeline's queue -- a render must never wait
behind an agent's utterance -- and the other direction holds too: the worker
yields to live speech, waiting before each sentence while any live utterance
is in flight or queued, so live speech is never delayed by more than the one
sentence already synthesising. Synthesis itself runs under a lock shared with
the live pipeline, so the two never reach the engine at the same time.

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
import subprocess
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

# The states a job passes through. Terminal ones are never left once entered;
# `paused` is not among them -- a job yielding to live speech runs on when the
# speech is over.
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

DEFAULT_SENTENCE_GAP_MS = 250
DEFAULT_CHAPTER_GAP_MS = 1500
DEFAULT_BITRATE = "64k"

# How often a paused worker re-checks whether live speech is still going.
# The bound on delaying live speech is one sentence's synthesis -- this only
# says how long a *finished* utterance may keep the render waiting past it,
# and a tenth of a second is inaudible either way.
DEFAULT_POLL_SECONDS = 0.1


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


# --- encoding ------------------------------------------------------------
#
# ffmpeg turns the per-part WAVs into the job's actual output. It is always
# run via `subprocess.run` with an argument list -- text comes from the job's
# metadata and titles, and a shell would make that an injection vector.

_STDERR_TAIL_CHARS = 4000  # enough for a human to see what failed, no more


def ffmpeg_available() -> bool:
    """Whether `ffmpeg` is on `PATH` -- what `status.render.available` reports."""
    return shutil.which("ffmpeg") is not None


class _EncodeFailed(Exception):
    """Internal signal: encoding could not produce `out`. Carries the reason
    (ffmpeg's stderr tail, or a message for a problem ffmpeg never saw, such
    as it being missing) for `_render` to put in `job.error`."""


def _concat_parts(job: RenderJob, work_dir: Path) -> tuple[Path, list[tuple[float, float, str]]]:
    """Concatenate the parts' WAVs into one file, `chapter_gap_ms` of silence
    between each, and return the chapters actually produced: `(start, end,
    title)` in seconds per part, measured from each part's real WAV duration
    and the gaps just written, never estimated.

    Built by appending raw PCM rather than through ffmpeg's concat demuxer:
    a part boundary is just a longer version of the gap `_synthesize` already
    writes between sentences, via the same `append_pcm`, so there is no
    second concatenation mechanism to keep in sync with the first.
    """
    sample_rate, _ = _wav_info(work_dir / "part-0.wav")
    concat_path = work_dir / "concat.wav"
    # Rebuilt from the part WAVs every time: a retry after a failed encode
    # must not append onto a stale concat file left by the attempt before it.
    concat_path.unlink(missing_ok=True)
    gap_frames = int(sample_rate * job.chapter_gap_ms / 1000)
    gap_pcm = b"\x00" * (gap_frames * 2)

    chapters: list[tuple[float, float, str]] = []
    cursor = 0.0
    for i, part in enumerate(job.parts):
        part_path = work_dir / f"part-{i}.wav"
        duration = wav_duration(part_path)
        append_pcm(concat_path, part_path.read_bytes()[_HEADER_SIZE:], sample_rate)
        chapters.append((cursor, cursor + duration, part.title))
        cursor += duration
        if i < len(job.parts) - 1 and job.chapter_gap_ms:
            append_pcm(concat_path, gap_pcm, sample_rate)
            cursor += job.chapter_gap_ms / 1000
    return concat_path, chapters


def _write_chapters_file(chapters: list[tuple[float, float, str]], path: Path) -> None:
    """An ffmetadata chapters file, milliseconds since `TIMEBASE=1/1000`."""
    lines = [";FFMETADATA1"]
    for start, end, title in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(start * 1000)}",
            f"END={round(end * 1000)}",
            f"title={title}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metadata_args(metadata: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key in ("title", "artist", "album", "date"):
        value = metadata.get(key)
        if value:
            args += ["-metadata", f"{key}={value}"]
    return args


def _encode(job: RenderJob, work_dir: Path) -> None:
    """Concatenate and encode the job's parts to `job.out`, tagged from
    `job.metadata`. Writes to `<out>.partial` and `os.replace`s it onto
    `out` only once ffmpeg has exited 0, so a reader of `out` never sees a
    half-written file and a failed attempt never leaves one behind.

    Raises `_EncodeFailed` -- never touches `out` or leaves a `.partial` --
    if ffmpeg is missing or exits nonzero. `_render` decides what that means
    for the job's state and work directory.
    """
    if not ffmpeg_available():
        raise _EncodeFailed("ffmpeg not found on PATH")

    concat_path, chapters = _concat_parts(job, work_dir)
    partial = job.out.with_name(job.out.name + ".partial")

    cmd = ["ffmpeg", "-y", "-i", str(concat_path)]
    if job.format == "m4b":
        chapters_path = work_dir / "chapters.txt"
        _write_chapters_file(chapters, chapters_path)
        # The chapters file is a second input purely to carry metadata: its
        # own (silent) audio stream is never mapped into the output.
        cmd += ["-i", str(chapters_path), "-map_metadata", "1", "-map_chapters", "1"]
    # Tags after any -map_metadata, so they win over whatever the chapters
    # file's own (empty) global metadata block would otherwise contribute.
    cmd += _metadata_args(job.metadata)

    if job.format == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", job.bitrate, "-ac", "1", "-f", "mp3"]
    elif job.format == "opus":
        cmd += ["-c:a", "libopus", "-b:a", job.bitrate, "-f", "opus"]
    elif job.format == "m4b":
        cmd += ["-c:a", "aac", "-b:a", job.bitrate, "-f", "mp4"]
    else:
        raise _EncodeFailed(f"unknown render format: {job.format!r}")
    cmd.append(str(partial))

    job.out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise _EncodeFailed(result.stderr[-_STDERR_TAIL_CHARS:])
    os.replace(partial, job.out)


# --- cancellation ------------------------------------------------------


class _Cancelled(Exception):
    """Internal signal: unwind `_render` without touching `out`."""


# --- the queue ---------------------------------------------------------


class RenderQueue:
    """Runs render jobs on one worker thread, in submission order.

    Two arguments tie the queue to the live pipeline it shares the engine
    with. `busy` says whether a live utterance is in flight or queued -- in
    the daemon it is `daemon.speaking` -- and while it is true the worker
    waits *before* each sentence, in the state `paused`, checking every
    `poll_seconds`. Waiting happens between sentences and never inside one,
    which is what bounds the delay a render can impose on live speech at one
    sentence's synthesis time. `synth_lock` is the lock the live pipeline's
    own synthesize calls take: a render sentence and a live sentence never
    reach the engine at the same time, and the lock the live side holds is
    held for one sentence, so neither waits on more than that.

    Both are optional because a queue of their own -- in tests, or beside a
    daemon that is not running one -- has nothing to yield to.
    """

    def __init__(
        self,
        engine: Synthesizer,
        synth_lock: threading.Lock | None = None,
        busy: Callable[[], bool] | None = None,
        work_root: Path | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        # One of their own when not shared: the lock has to exist for the
        # synthesize call to take it, but nothing else can ever contend one
        # nobody was handed.
        self._synth_lock = synth_lock if synth_lock is not None else threading.Lock()
        self._busy = busy
        self._poll_seconds = poll_seconds
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
            if job.state in ("running", "paused"):
                # A paused job is a running job that is yielding: it is in
                # `_render`'s hands, so it is flagged and unwinds on its own
                # check, exactly as a mid-sentence one does. Without this a
                # render could not be cancelled for as long as live speech
                # went on -- which is most of a busy day.
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
            _encode(job, work_dir)
        except _Cancelled:
            self._cancelled.discard(job.id)
            shutil.rmtree(work_dir, ignore_errors=True)
            # The terminal state is written last, after the work directory is
            # gone: a watcher that polls the state and then looks at the disk
            # -- a test, or a client cleaning up -- must never see "cancelled"
            # with the files of a cancelled render still on it.
            job.state = "cancelled"
            return
        except _EncodeFailed as exc:
            # The work directory survives a failed encode -- every part WAV
            # is still on disk, so a retry re-encodes instead of
            # re-synthesising. `_encode` itself guarantees no `.partial` or
            # `out` was left behind on this path.
            job.error = str(exc)
            save_manifest(job, work_dir)
            job.state = "failed"
            return

        shutil.rmtree(work_dir, ignore_errors=True)
        job.state = "done"

    def _yield_to_live(self, job: RenderJob) -> None:
        """Wait, before a sentence, while live speech is in flight or queued.

        Checked before *every* sentence and never during one: the promise to
        live speech is that it waits on at most the sentence already being
        synthesised, and a yield inside a sentence would mean interrupting
        the engine mid-call, which no engine here can do. The state is the
        `paused` the render event publishes, so a watcher can tell a render
        that is yielding from one that is working.
        """
        if self._busy is None or not self._busy():
            return
        job.state = "paused"
        # `_stopping` breaks the wait rather than widening it: `stop()` joins
        # this thread, so a worker paused behind speech that never ends would
        # hang the join until its timeout with the job no further along.
        while self._busy() and not self._stopping:
            if job.id in self._cancelled:
                raise _Cancelled
            time.sleep(self._poll_seconds)
        job.state = "running"

    def _synthesize(self, job: RenderJob, work_dir: Path) -> None:
        for part_i in range(job.part_index, len(job.parts)):
            part = job.parts[part_i]
            sentences = _sentences(part.text)
            start = job.sentence_index if part_i == job.part_index else 0
            wav_path = work_dir / f"part-{part_i}.wav"
            for sent_i in range(start, len(sentences)):
                if job.id in self._cancelled:
                    raise _Cancelled
                self._yield_to_live(job)
                text = sentences[sent_i]
                # The lock, and only the lock, around the engine call: the
                # live pipeline's producer takes the same one, so a live
                # sentence never runs against a render sentence -- and never
                # waits on more of the render than this one call.
                with self._synth_lock:
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
