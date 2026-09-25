"""Renders long text to audio files in the background, off live speech.

A render is a queue of `RenderJob`s worked one at a time by a dedicated
thread. It never shares the live pipeline's queue -- a render must never wait
behind an agent's utterance -- and the other direction holds too: the worker
yields to live speech, waiting before each sentence while any live utterance
is in flight or queued, so live speech is never delayed by more than the one
sentence already synthesising. Synthesis itself runs under a lock shared with
the live pipeline, so the two never reach the engine at the same time.

Each job's progress survives a crash: a work directory holds a manifest
(the parts, and the next part and sentence to synthesise) and one headerless
PCM file per part, appended to as each sentence finishes and never rewritten
from scratch. A daemon that restarts mid-render re-queues the job and picks
up exactly where the manifest says, without re-synthesising anything already
on disk.

Parts are stored as raw s16le mono PCM rather than WAV: a WAV's RIFF size
fields are 32-bit, capping a part at 4 GiB -- about 24.8 hours at Kokoro's
24 kHz -- before the header itself starts lying about how much data follows.
An audiobook is not bounded that way, so nothing here keeps a header that
could go stale; a part's duration is always its file size divided by the
sample rate, computed fresh rather than cached anywhere that could drift
from what is actually on disk.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

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
    # The rate every part's PCM is stored at, in Hz. 0 until `RenderQueue`
    # sets it (from its engine) the first time this job is submitted or
    # resumed -- see `RenderQueue._ensure_sample_rate`. Recorded here rather
    # than re-read from the engine at encode time, because encoding runs
    # from a manifest that must describe its own PCM regardless of what
    # engine the daemon currently happens to be running.
    sample_rate: int = 0
    metadata: dict[str, str] = field(default_factory=dict)
    sentence_gap_ms: int = DEFAULT_SENTENCE_GAP_MS
    chapter_gap_ms: int = DEFAULT_CHAPTER_GAP_MS
    bitrate: str = DEFAULT_BITRATE
    state: str = "queued"
    # Where to resume: the next part to synthesise, and the next sentence
    # within it. Parts before `part_index` are complete and never revisited.
    part_index: int = 0
    sentence_index: int = 0
    # The PCM byte length of `part-{part_index}.pcm` as of the last sentence
    # actually recorded in this manifest -- reset to 0 whenever `part_index`
    # advances. What resume truncates the part file back to, so a crash
    # between an append and the manifest save that names it can never
    # duplicate a sentence. See `_truncate_to_committed`.
    committed_bytes: int = 0
    done_seconds: float = 0.0
    estimate_seconds: float = 0.0
    error: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        """Everything but the parts' text -- see `to_parts`.

        Written on every sentence, so it must never grow with a render's
        length: this is progress (which part and sentence, how many bytes
        committed, timing, state, error) and job metadata, none of it sized
        by how much text a job holds. `parts` is deliberately absent; a
        caller that needs the parts back reads them from `parts.json`
        (`save_manifest`/`load_manifest` do this pairing), or, for an old
        manifest written before this split, from this same dict, which is
        why `from_manifest` still accepts one with `parts` in it.
        """
        data = dict(self.__dict__)
        del data["parts"]
        data["out"] = str(self.out)
        return data

    def to_parts(self) -> list[dict[str, str]]:
        """The parts' titles and text -- written once, at submit, to
        `parts.json`, and never rewritten: this is the piece of a render
        that scales with a book's length, so nothing may touch it again on
        every sentence the way the progress manifest is."""
        return [{"title": p.title, "text": p.text} for p in self.parts]

    @classmethod
    def from_manifest(cls, data: dict[str, Any], parts: list[Part]) -> RenderJob:
        kwargs = dict(data)
        # Compatibility with a manifest written before parts.json existed:
        # its own embedded `parts` wins over whatever `parts` this call was
        # given, since it is the only copy that record ever had.
        if "parts" in kwargs:
            parts = [Part(**p) for p in kwargs.pop("parts")]
        kwargs["parts"] = parts
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


def _forget_text(job: RenderJob) -> None:
    """Drop a terminal job's part text from memory, keeping only titles.

    Once a job is `done`, `failed` or `cancelled` nothing will ever
    synthesise from it again -- `done` and `cancelled` have already lost
    their work directory, and a retried `failed` job resumes from its own
    `parts.json` on disk, not from this in-memory copy. A daemon that has
    rendered a lot of long books has no reason to keep megabytes of their
    text sitting in `RenderQueue._jobs` after the fact.
    """
    job.parts = [Part(title=p.title, text="") for p in job.parts]


# --- the manifest on disk -------------------------------------------------


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / "manifest.json"


def _parts_path(work_dir: Path) -> Path:
    return work_dir / "parts.json"


def save_manifest(job: RenderJob, work_dir: Path) -> None:
    """Write the manifest atomically, so a reader never sees a half-written one.

    `parts.json` -- the titles and text, the part of a render that scales
    with a book's length -- is written once, the first time a job's work
    directory sees a manifest at all, and never again: every later call
    here (one per sentence, for as long as a render runs) only rewrites
    `manifest.json`, which holds nothing bigger than progress. Without this
    split, a manifest save's cost would grow with the render's own text,
    turning an hours-long audiobook into gigabytes of repeated writes --
    exactly what the no-length-limit requirement rules out.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    parts_path = _parts_path(work_dir)
    if not parts_path.exists():
        parts_tmp = work_dir / "parts.json.tmp"
        parts_tmp.write_text(json.dumps(job.to_parts()), encoding="utf-8")
        os.replace(parts_tmp, parts_path)
    tmp = work_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(job.to_manifest(), indent=2), encoding="utf-8")
    os.replace(tmp, _manifest_path(work_dir))


def load_manifest(work_dir: Path) -> RenderJob:
    """The inverse of `save_manifest`: `manifest.json` plus `parts.json`.

    A manifest written before the split carries its own `parts` embedded,
    and `RenderJob.from_manifest` prefers that over `parts.json` (which
    will not exist for one) -- old manifests load exactly as they always
    did.
    """
    data = json.loads(_manifest_path(work_dir).read_text(encoding="utf-8"))
    parts_path = _parts_path(work_dir)
    parts: list[Part] = []
    if "parts" not in data and parts_path.exists():
        parts = [Part(**p) for p in json.loads(parts_path.read_text(encoding="utf-8"))]
    return RenderJob.from_manifest(data, parts)


# --- PCM parts, appended to one sentence at a time -------------------------
#
# Headerless s16le mono PCM: no RIFF size field to keep in sync and no 4 GiB
# ceiling for it to overflow at. Duration is always computed from the file's
# own size, so it can never go stale the way a cached or embedded one could.


def append_pcm(path: Path, pcm: bytes) -> None:
    """Append 16-bit mono PCM to `path`, creating it if new."""
    with path.open("ab") as fh:
        fh.write(pcm)


def pcm_duration(path: Path, sample_rate: int) -> float:
    """Seconds of audio in a headerless PCM file written by `append_pcm`."""
    if not path.exists():
        return 0.0
    return path.stat().st_size / (sample_rate * 2)


def _truncate_to_committed(path: Path, committed_bytes: int) -> None:
    """Truncate `path` back to the byte length recorded with the last
    sentence actually committed to the manifest.

    A crash (or a plain kill -9) can land between `append_pcm` finishing its
    write and `save_manifest` recording the new length: the PCM for a
    sentence is on disk, but the manifest still names the sentence before
    it. Resuming from the manifest's sentence index would then re-synthesise
    and re-append that same sentence, duplicating it in the output. Cutting
    the file back to exactly the length the manifest last recorded removes
    any such orphaned tail -- whether it is a whole sentence or (the old
    concern this replaces) a single dangling byte from a `write()` caught
    mid-call, 16-bit mono meaning every sample is 2 bytes.

    A file already no longer than that length is left alone: nothing to cut,
    and shrinking further would be a bug in the caller, not a crash.
    """
    if not path.exists():
        return
    size = path.stat().st_size
    if size > committed_bytes:
        with path.open("r+b") as fh:
            fh.truncate(committed_bytes)


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
# ffmpeg turns the per-part PCM files into the job's actual output. It is
# always run via `subprocess.Popen` with an argument list -- text comes from
# the job's metadata and titles, and a shell would make that an injection
# vector -- and the audio reaches it over a pipe rather than a file: nothing
# about a render's length is ever held in memory or written twice.

_STDERR_TAIL_CHARS = 4000  # enough for a human to see what failed, no more

# Bytes moved per read/write while streaming a part to ffmpeg's stdin. Bounds
# memory use to this regardless of a part's length -- the whole point of
# storing parts as PCM in the first place.
_CHUNK_BYTES = 1024 * 1024


def ffmpeg_available() -> bool:
    """Whether `ffmpeg` is on `PATH` -- what `status.render.available` reports."""
    return shutil.which("ffmpeg") is not None


class _EncodeFailed(Exception):
    """Internal signal: encoding could not produce `out`. Carries the reason
    (ffmpeg's stderr tail, or a message for a problem ffmpeg never saw, such
    as it being missing) for `_render` to put in `job.error`."""


def _stream_file(path: Path, sink: IO[bytes]) -> None:
    """Write `path` to `sink` in `_CHUNK_BYTES` pieces, never the whole file
    at once -- the read side of the same bound `_CHUNK_BYTES` names."""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                return
            sink.write(chunk)


def _written_parts(job: RenderJob, work_dir: Path) -> list[tuple[int, Part]]:
    """Parts that actually produced a PCM file, in order.

    A part whose text yields no sentences at render time (blank after all,
    or text that segments to nothing) is skipped cleanly here rather than
    treated as an error: no PCM file for it is expected, not a fault, and it
    contributes no chapter and no gap either side of the one it would have
    been.
    """
    return [(i, part) for i, part in enumerate(job.parts) if (work_dir / f"part-{i}.pcm").exists()]


def _chapter_plan(job: RenderJob, work_dir: Path) -> list[tuple[float, float, str]]:
    """`(start, end, title)` in seconds per part, from each part's real PCM
    size on disk and the gaps `_write_audio_stream` writes between them --
    never estimated, and computed without reading any part's audio."""
    written = _written_parts(job, work_dir)
    chapters: list[tuple[float, float, str]] = []
    cursor = 0.0
    for j, (i, part) in enumerate(written):
        duration = pcm_duration(work_dir / f"part-{i}.pcm", job.sample_rate)
        chapters.append((cursor, cursor + duration, part.title))
        cursor += duration
        if j < len(written) - 1 and job.chapter_gap_ms:
            cursor += job.chapter_gap_ms / 1000
    return chapters


def _write_audio_stream(
    job: RenderJob,
    work_dir: Path,
    sink: IO[bytes],
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Write every part's PCM to `sink`, `chapter_gap_ms` of silence between
    each -- streamed through `_stream_file`, so a part's whole length is
    never held in memory at once. A part boundary is just a longer version
    of the gap `_synthesize` already writes between sentences.

    Checked between parts, the same "never inside one unit of work" rule
    `_synthesize` follows between sentences: `cancelled`, if given, is
    polled once per part, so a cancel reaches ffmpeg within one part's
    streaming time rather than waiting for the whole encode to finish.
    """
    written = _written_parts(job, work_dir)
    gap_frames = int(job.sample_rate * job.chapter_gap_ms / 1000)
    gap_pcm = b"\x00" * (gap_frames * 2)
    for j, (i, _part) in enumerate(written):
        if cancelled is not None and cancelled():
            raise _Cancelled
        _stream_file(work_dir / f"part-{i}.pcm", sink)
        if j < len(written) - 1 and job.chapter_gap_ms:
            sink.write(gap_pcm)


# ffmpeg's own ffmetadata rule: a literal `=`, `;`, `#`, `\` or newline in a
# value must be backslash-escaped, backslash first so an already-escaped
# character is never escaped twice. A chapter title is arbitrary text --
# whatever a book's own table of contents says -- so any of these can show
# up in one for real.
_FFMETADATA_ESCAPE = (("\\", "\\\\"), ("=", "\\="), (";", "\\;"), ("#", "\\#"), ("\n", "\\\n"))


def _escape_ffmetadata(value: str) -> str:
    for char, escaped in _FFMETADATA_ESCAPE:
        value = value.replace(char, escaped)
    return value


def _write_chapters_file(chapters: list[tuple[float, float, str]], path: Path) -> None:
    """An ffmetadata chapters file, milliseconds since `TIMEBASE=1/1000`."""
    lines = [";FFMETADATA1"]
    for start, end, title in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(start * 1000)}",
            f"END={round(end * 1000)}",
            f"title={_escape_ffmetadata(title)}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metadata_args(metadata: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key in ("title", "artist", "album", "date"):
        value = metadata.get(key)
        if value:
            args += ["-metadata", f"{key}={value}"]
    return args


def _encode(job: RenderJob, work_dir: Path, cancelled: Callable[[], bool] | None = None) -> None:
    """Concatenate and encode the job's parts to `job.out`, tagged from
    `job.metadata`. Writes to `<out>.partial` and `os.replace`s it onto
    `out` only once ffmpeg has exited 0, so a reader of `out` never sees a
    half-written file and a failed attempt never leaves one behind.

    Raises `_EncodeFailed` -- never touches `out` or leaves a `.partial` --
    if ffmpeg is missing or exits nonzero. `_render` decides what that means
    for the job's state and work directory. Raises `_Cancelled` -- also
    never touching `out`, and killing ffmpeg first -- if `cancelled` starts
    reporting true while streaming. On *any* other exception while streaming
    (a read error on a part's PCM, for instance), ffmpeg is killed and
    waited for and `.partial` removed before the exception is re-raised:
    an encode must never leave a subprocess or a half-written file behind
    just because something upstream broke.
    """
    if not ffmpeg_available():
        raise _EncodeFailed("ffmpeg not found on PATH")

    chapters = _chapter_plan(job, work_dir)
    partial = job.out.with_name(job.out.name + ".partial")
    # ffmpeg's stderr goes to a file, not a pipe: reading a pipe requires
    # draining it concurrently with writing stdin or the two can deadlock
    # each other on a full OS buffer, and a file sidesteps that entirely.
    stderr_path = work_dir / "ffmpeg-stderr.log"

    cmd = ["ffmpeg", "-y", "-f", "s16le", "-ar", str(job.sample_rate), "-ac", "1", "-i", "pipe:0"]
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
        # No `-movflags +faststart`: moving the moov atom to the front is a
        # second full rewrite of the finished file, which would double disk
        # use for exactly the long files this streaming path exists for.
        # It only matters for playback that starts before the file has
        # fully downloaded, which is not this file's use case -- a podcast
        # or audiobook app reads an m4b's chapters and tags from the
        # trailer just as well.
        cmd += ["-c:a", "aac", "-b:a", job.bitrate, "-f", "mp4"]
    else:
        raise _EncodeFailed(f"unknown render format: {job.format!r}")
    cmd.append(str(partial))

    job.out.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_file
        )
        assert process.stdin is not None
        try:
            _write_audio_stream(job, work_dir, process.stdin, cancelled)
        except BrokenPipeError:
            # ffmpeg exited before consuming everything -- a bad codec, a
            # full disk. The exit code and stderr below say why; this only
            # stops the write loop from raising past it.
            pass
        except Exception:
            # A cancel, or anything else gone wrong mid-stream: ffmpeg gets
            # no more input than it has already seen and is not left to run
            # on its own. Killed rather than asked to finish -- there is no
            # reason to wait out an encode nothing will use the result of.
            process.kill()
            try:
                process.stdin.close()
            except OSError:
                pass
            process.wait()
            partial.unlink(missing_ok=True)
            raise
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        returncode = process.wait()
    if returncode != 0:
        partial.unlink(missing_ok=True)
        tail = stderr_path.read_bytes()[-_STDERR_TAIL_CHARS:]
        raise _EncodeFailed(tail.decode("utf-8", errors="replace"))
    os.replace(partial, job.out)


# --- cancellation ------------------------------------------------------


class _Cancelled(Exception):
    """Internal signal: unwind `_render` without touching `out`."""


class _Stopping(Exception):
    """Internal signal: the queue is stopping. Unwind `_render` leaving the
    job exactly as it was -- still `running`, resumable from its manifest --
    so a normal daemon shutdown mid-append never turns into a failed or
    cancelled job, only one picked up again on the next `resume()`."""


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
        on_update: Callable[[RenderJob], None] | None = None,
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
        # Called with the job on every state change and every sentence's
        # progress, so a caller (the daemon) can publish a `render` event
        # without this module knowing anything about events or throttling --
        # see "Global Constraints (A)" in the audio-export plan, which puts
        # the throttling at the verb/event layer, not here.
        self._on_update = on_update
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

    def _notify(self, job: RenderJob) -> None:
        """Tell `on_update` about the job's current state, if anyone is listening.

        Guarded: a subscriber's own bug (a bad event handler, a dead bus)
        must cost the render nothing. This runs on the worker thread, whose
        one job is to keep synthesising and encoding.
        """
        if self._on_update is None:
            return
        try:
            self._on_update(job)
        except Exception:
            pass

    def _ensure_sample_rate(self, job: RenderJob) -> None:
        """Fix the rate a job's PCM is stored at, the first time it is ever
        queued. Left alone once set: a resumed job's already-written PCM
        was made at whatever rate this recorded the first time, and nothing
        may make that field disagree with the bytes already on disk.
        """
        if not job.sample_rate:
            job.sample_rate = self._engine.sample_rate

    def submit(self, job: RenderJob) -> str:
        self._ensure_sample_rate(job)
        save_manifest(job, self._work_dir(job.id))
        with self._lock:
            self._jobs[job.id] = job
            self._queue.append(job.id)
        self._wake.set()
        self._notify(job)
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
            if job.state == "failed":
                # Not re-run -- a failure needs a person's attention, not
                # another automatic attempt -- but surfaced in `status` and
                # reachable by `render_cancel`, which deletes its work
                # directory: a failed render must not strand disk space
                # forever just because nothing asked about it again.
                with self._lock:
                    self._jobs[job.id] = job
                self._notify(job)
                continue
            if job.state in _TERMINAL_STATES:
                continue
            if job.sample_rate and job.sample_rate != self._engine.sample_rate:
                # The PCM already on disk was written at the old rate; going
                # on would either mis-decode it at encode time or silently
                # mix rates within one output. Neither is recoverable here --
                # the engine that made this daemon's rate what it is has
                # changed -- so the job is surfaced as failed rather than
                # producing a wrong file.
                job.error = (
                    f"engine sample rate changed from {job.sample_rate} to "
                    f"{self._engine.sample_rate}; this render cannot be resumed"
                )
                job.state = "failed"
                _forget_text(job)
                save_manifest(job, entry)
                with self._lock:
                    self._jobs[job.id] = job
                self._notify(job)
                continue
            job.state = "queued"
            self._ensure_sample_rate(job)
            save_manifest(job, entry)
            with self._lock:
                self._jobs[job.id] = job
                self._queue.append(job.id)
            self._notify(job)
        self._wake.set()

    def cancel(self, job_id: str) -> bool:
        """Stop a job and remove its work directory.

        A queued job still sitting in the queue is simply dropped, without
        disturbing whatever is currently running. A failed job is not
        running anything, but its work directory is still on disk (kept for
        a retry that never came); this deletes it, so `render_cancel` also
        serves as the way to reclaim a stranded failure's space. Anything
        else still short of a terminal state -- running, paused, or queued
        but already handed to the worker thread (dequeued, `_render` not
        yet reached the point of marking it `running`) -- is flagged, and
        `_render`/`_synthesize`/`_encode` unwind and clean up on their own
        the next time they check -- never from this thread, which does not
        own the job's files while they are working.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            dequeued = False
            failed = False
            flagged = False
            if job_id in self._queue:
                self._queue.remove(job_id)
                job.state = "cancelled"
                _forget_text(job)
                dequeued = True
            elif job.state == "failed":
                failed = True
            elif job.state in ("queued", "running", "paused"):
                # Covers a job the worker has already dequeued but not yet
                # marked `running` (still `queued` here, but no longer in
                # `self._queue`), as well as one actually running or paused
                # behind live speech, and one encoding -- `_encode` polls
                # this same set. Without "queued" here, a job could pass
                # through a window where it is neither in the queue nor
                # cancellable.
                self._cancelled.add(job_id)
                flagged = True
        if dequeued or failed:
            shutil.rmtree(self._work_dir(job_id), ignore_errors=True)
        if dequeued:
            self._notify(job)
        return dequeued or failed or flagged

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
        self._notify(job)
        try:
            self._synthesize(job, work_dir)
            _encode(job, work_dir, cancelled=lambda: job.id in self._cancelled)
        except _Cancelled:
            self._cancelled.discard(job.id)
            shutil.rmtree(work_dir, ignore_errors=True)
            # The terminal state is written last, after the work directory is
            # gone: a watcher that polls the state and then looks at the disk
            # -- a test, or a client cleaning up -- must never see "cancelled"
            # with the files of a cancelled render still on it.
            job.state = "cancelled"
            _forget_text(job)
            self._notify(job)
            return
        except _Stopping:
            # Leave the job exactly as `_synthesize` left it -- still
            # `running`, its manifest already durable up to the last
            # committed sentence. `resume()` picks it up on the next start;
            # nothing here needs to touch state, the work directory or
            # `_cancelled`.
            return
        except _EncodeFailed as exc:
            # The work directory survives a failed encode -- every part WAV
            # is still on disk, so a retry re-encodes instead of
            # re-synthesising. `_encode` itself guarantees no `.partial` or
            # `out` was left behind on this path.
            job.error = str(exc)
            save_manifest(job, work_dir)
            job.state = "failed"
            _forget_text(job)
            self._cancelled.discard(job.id)
            self._notify(job)
            return
        except Exception as exc:  # noqa: BLE001 - a render's own worker thread
            # must survive whatever any one job throws: an engine error, a
            # bad `out` path, a full disk mid-write -- anything not already
            # named above. Without this, one bad job would silently end the
            # worker thread and strand every job queued behind it.
            job.error = str(exc)
            try:
                save_manifest(job, work_dir)
            except OSError:
                pass
            job.state = "failed"
            _forget_text(job)
            self._cancelled.discard(job.id)
            self._notify(job)
            return

        shutil.rmtree(work_dir, ignore_errors=True)
        job.state = "done"
        _forget_text(job)
        self._cancelled.discard(job.id)
        self._notify(job)

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
        self._notify(job)
        # `_stopping` breaks the wait, raising `_Stopping` to unwind the job
        # cleanly rather than proceeding to synthesise: `stop()` joins this
        # thread, so a worker paused behind speech that never ends would
        # otherwise hang the join until its timeout with the job no further
        # along, and a worker that pressed on regardless could still die
        # mid-append if the process is killed the moment `stop()` returns.
        while self._busy():
            if job.id in self._cancelled:
                raise _Cancelled
            if self._stopping:
                raise _Stopping
            time.sleep(self._poll_seconds)
        job.state = "running"
        self._notify(job)

    def _wait_for_load(self, job: RenderJob) -> None:
        """Wait, before a sentence, while the engine is not loaded.

        `LazyEngine.synthesize` on an unloaded engine returns silence rather
        than raising or blocking -- so without this, a render started (or
        resumed) while the model is unloaded or still loading would "finish"
        having spoken nothing at all. Duck-typed: an engine with nothing to
        say about `loaded` (there is none today) is read as always loaded,
        exactly as `_supported_languages` reads a silent engine as speaking
        everything. Same shape as `_yield_to_live`: state `paused`, the same
        poll, and cancellable and stoppable the same way.
        """
        if getattr(self._engine, "loaded", True):
            return
        job.state = "paused"
        self._notify(job)
        while not getattr(self._engine, "loaded", True):
            if job.id in self._cancelled:
                raise _Cancelled
            if self._stopping:
                raise _Stopping
            time.sleep(self._poll_seconds)
        job.state = "running"
        self._notify(job)

    def _synthesize(self, job: RenderJob, work_dir: Path) -> None:
        for part_i in range(job.part_index, len(job.parts)):
            part = job.parts[part_i]
            sentences = _sentences(part.text)
            start = job.sentence_index if part_i == job.part_index else 0
            pcm_path = work_dir / f"part-{part_i}.pcm"
            # Only ever cuts anything when resuming mid-part after a crash
            # between an append and the manifest save that recorded it, but
            # cheap enough (one stat, and a truncate only then) to run
            # unconditionally rather than have a second code path for it.
            _truncate_to_committed(pcm_path, job.committed_bytes)
            for sent_i in range(start, len(sentences)):
                if job.id in self._cancelled:
                    raise _Cancelled
                if self._stopping:
                    raise _Stopping
                self._wait_for_load(job)
                self._yield_to_live(job)
                text = sentences[sent_i]
                # The lock, and only the lock, around the engine call: the
                # live pipeline's producer takes the same one, so a live
                # sentence never runs against a render sentence -- and never
                # waits on more of the render than this one call.
                with self._synth_lock:
                    # A manifest written before renders carried a resolved language
                    # has `lang` empty; English is what those were spoken in.
                    audio = self._engine.synthesize(text, job.voice, job.speed, job.lang or "en")
                append_pcm(pcm_path, _pcm16(audio))
                job.done_seconds += len(audio) / self._engine.sample_rate
                is_last_sentence = sent_i == len(sentences) - 1
                if not is_last_sentence and job.sentence_gap_ms:
                    gap_frames = int(self._engine.sample_rate * job.sentence_gap_ms / 1000)
                    append_pcm(pcm_path, b"\x00" * (gap_frames * 2))
                job.sentence_index = sent_i + 1
                # Recorded from the file's own size, never accumulated, so it
                # can never drift from what `append_pcm` actually committed --
                # the same principle `pcm_duration` follows for duration.
                job.committed_bytes = pcm_path.stat().st_size
                save_manifest(job, work_dir)
                self._notify(job)
            job.part_index = part_i + 1
            job.sentence_index = 0
            job.committed_bytes = 0
            save_manifest(job, work_dir)
            self._notify(job)
