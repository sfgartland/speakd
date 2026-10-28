"""Measure the two Kokoro engines against each other.

Not shipped and not imported by the package: a development tool, run by hand.

Three kinds of number, deliberately taken in different ways.

RTF is taken twice. `paired` loads both engines into one process and alternates
them over the same texts, which is the fair head-to-head: identical machine
state, identical inputs, no scheduling luck for one and not the other. `isolated`
runs one engine in a fresh process, which is the number a daemon actually lives
with.

Resident memory and cold start only mean anything in isolation, so they come
from `isolated` alone. A process holding both engines tells you nothing about
what either costs on its own.

Installed size is attributed from each distribution's RECORD over the transitive
requirement closure. Shared packages are reported separately rather than charged
to both engines, because the interesting figure is what each one *adds*.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import wave
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

import numpy as np

VOICE = "af_heart"
SPEED = 1.1

# Sentences 1-3 nest, so the three long cases are the same prose growing:
# 77, then 77+1+68 = 146, then 146+1+66 = 213 characters.
_S1 = "The transcendental question asks how knowledge is possible, not what we know."
_S2 = "That answer cannot be drawn from experience, since it conditions it."
_S3 = "Hence the deduction argues where an empirical psychology observes."

RTF_TEXTS: tuple[tuple[str, str], ...] = (
    ("len4", "Yes."),
    ("len19", "The answer is four."),
    ("len77", _S1),
    ("len146", f"{_S1} {_S2}"),
    ("len213", f"{_S1} {_S2} {_S3}"),
)

WAV_TEXTS: tuple[tuple[str, str], ...] = (
    ("01-ordinary", "The pipeline speaks each segment as soon as it is ready."),
    ("02-abbreviations-symbols", "See §12, e.g. pp. 34-38, ca. 1787."),
    (
        "03-proper-nouns",
        "Kant's transcendental deduction, in the B edition, differs from Husserl's.",
    ),
    ("04-citation-key", "Stiegler 1994, Technics and Time, volume 1."),
)

# The single call each engine is given before timing starts, and whose result is
# thrown away: the first call pays for lazy imports, dictionary loads and, on the
# ONNX side, onnxruntime's first-inference graph setup.
WARMUP_TEXT = "Warming up the engine before anything is timed."

ENGINES = ("torch", "onnx")


def build(engine: str) -> Any:
    if engine == "torch":
        from speakd.synth.kokoro_engine import KokoroEngine

        return KokoroEngine()
    if engine == "onnx":
        from speakd.synth.kokoro_onnx_engine import KokoroOnnxEngine

        return KokoroOnnxEngine()
    raise SystemExit(f"unknown engine {engine!r}; expected one of {ENGINES}")


def resident_bytes() -> int:
    """Resident set size from /proc/self/statm, field 2, in bytes."""
    fields = Path("/proc/self/statm").read_text().split()
    return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


def rtf_run(engine: Any, text: str) -> dict[str, float]:
    start = time.perf_counter()
    audio = engine.synthesize(text, voice=VOICE, speed=SPEED)
    elapsed = time.perf_counter() - start
    seconds = len(audio) / engine.sample_rate if len(audio) else 0.0
    return {
        "chars": len(text),
        "synth_s": elapsed,
        "audio_s": seconds,
        "samples": int(len(audio)),
        "rtf": (elapsed / seconds) if seconds else float("nan"),
    }


def rtf_suite(engine: Any) -> dict[str, dict[str, float]]:
    return {name: rtf_run(engine, text) for name, text in RTF_TEXTS}


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """16-bit PCM, so the files play anywhere without a converter."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def cmd_isolated(args: argparse.Namespace) -> int:
    """One engine, fresh process: cold start, resident memory, RTF."""
    rss_start = resident_bytes()

    load_start = time.perf_counter()
    engine = build(args.engine)
    load_s = time.perf_counter() - load_start
    rss_loaded = resident_bytes()

    # Cold start is load plus the first synthesis: what a daemon restart costs
    # before any sound comes out. It is measured on the warmup call, which is
    # then discarded for RTF purposes.
    first_start = time.perf_counter()
    engine.synthesize(WARMUP_TEXT, voice=VOICE, speed=SPEED)
    first_synth_s = time.perf_counter() - first_start

    suite = rtf_suite(engine)
    rss_synth = resident_bytes()

    json.dump(
        {
            "engine": args.engine,
            "name": engine.name,
            "sample_rate": engine.sample_rate,
            "load_s": load_s,
            "first_synth_s": first_synth_s,
            "cold_start_to_first_audio_s": load_s + first_synth_s,
            "rss_start_bytes": rss_start,
            "rss_after_load_bytes": rss_loaded,
            "rss_after_synth_bytes": rss_synth,
            "rtf": suite,
        },
        sys.stdout,
        indent=2,
    )
    return 0


def cmd_paired(args: argparse.Namespace) -> int:
    """Both engines in one process, alternating over the same texts."""
    engines = {name: build(name) for name in ENGINES}
    for engine in engines.values():
        engine.synthesize(WARMUP_TEXT, voice=VOICE, speed=SPEED)

    rounds: list[dict[str, Any]] = []
    for _ in range(args.rounds):
        measured: dict[str, Any] = {}
        for name, text in RTF_TEXTS:
            for engine_name in ENGINES:
                measured.setdefault(engine_name, {})[name] = rtf_run(engines[engine_name], text)
        rounds.append(measured)

    json.dump({"rounds": rounds}, sys.stdout, indent=2)
    return 0


def cmd_wavs(args: argparse.Namespace) -> int:
    """Write both engines' audio for the comparison texts, plus a summary."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict[str, Any]] = {}
    for engine_name in ENGINES:
        engine = build(engine_name)
        engine.synthesize(WARMUP_TEXT, voice=VOICE, speed=SPEED)
        for label, text in WAV_TEXTS:
            audio = engine.synthesize(text, voice=VOICE, speed=SPEED)
            path = out / f"{label}__{engine_name}.wav"
            write_wav(path, audio, engine.sample_rate)
            summary.setdefault(label, {"text": text})[engine_name] = describe(
                audio, engine.sample_rate
            )

    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    json.dump(summary, sys.stdout, indent=2)
    return 0


def describe(audio: np.ndarray, sample_rate: int) -> dict[str, Any]:
    """Waveform facts that can be stated without listening."""
    if not len(audio):
        return {"samples": 0, "seconds": 0.0}
    magnitude = np.abs(audio)
    # "Silence" at a threshold well under speech level but above dither.
    speech = magnitude > 0.01
    leading = int(np.argmax(speech)) if speech.any() else len(audio)
    trailing = int(len(audio) - 1 - np.argmax(speech[::-1])) if speech.any() else 0
    return {
        "samples": int(len(audio)),
        "seconds": len(audio) / sample_rate,
        "peak": float(magnitude.max()),
        "rms": float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))),
        "lead_silence_s": leading / sample_rate,
        "trail_silence_s": (len(audio) - 1 - trailing) / sample_rate,
        "voiced_s": (trailing - leading + 1) / sample_rate if speech.any() else 0.0,
    }


# --- what the two engines actually say ---------------------------------------

# misaki does not consume espeak's IPA as-is: `misaki.espeak.EspeakFallback`
# rewrites it into its own convention before the model sees it. Comparing the
# two engines' raw phoneme strings therefore reports dozens of differences that
# are pure notation. This mirrors that rewrite so a diff shows only what the
# two would genuinely pronounce differently.
_E2M: tuple[tuple[str, str], ...] = (
    ("\u025cː\u0279", "\u025c\u0279"),
    ("\u025cː", "\u025c\u0279"),
    ("\u026aə", "iə"),
    ("aɪ", "I"),
    ("aʊ", "W"),
    ("dʒ", "ʤ"),
    ("eɪ", "A"),
    ("tʃ", "ʧ"),
    ("ɔɪ", "Y"),
    ("oʊ", "O"),
    ("ɚ", "ə\u0279"),
    ("ɐ", "ə"),
    ("r", "\u0279"),
)


def to_misaki_convention(phonemes: str) -> str:
    """Rewrite espeak IPA the way misaki does, so a diff means something."""
    for old, new in _E2M:
        phonemes = phonemes.replace(old, new)
    return phonemes.replace("ː", "").replace("ɾ", "T").replace("ʔ", "t")


def without_marks(phonemes: str) -> str:
    """Drop stress marks and punctuation: compare segments, not prosody."""
    return re.sub(r"[ˈˌ.,!?;:]", "", phonemes)


def cmd_phonemes(args: argparse.Namespace) -> int:
    """Print what each engine would say, before and after the pronunciation table."""
    import kokoro
    import kokoro_onnx

    from speakd.model import Piece, Span
    from speakd.synth.kokoro_engine import NO_SPLIT
    from speakd.synth.kokoro_onnx_engine import resolve_model_files
    from speakd.transforms.pronunciation import pronunciation

    pipeline = kokoro.KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    model, voices = resolve_model_files()
    onnx = kokoro_onnx.Kokoro(str(model), str(voices))

    def misaki_of(text: str) -> str:
        return " ".join(
            ps for _, ps, _ in pipeline(text, voice=VOICE, speed=SPEED, split_pattern=NO_SPLIT)
        )

    def transformed(text: str) -> str:
        return pronunciation([Piece(span=Span(0, len(text)), spoken=text)])[0].spoken

    for label, text in (*WAV_TEXTS, *RTF_TEXTS):
        spoken = transformed(text)
        print("=" * 78)
        print(f"{label}")
        print(f"  text        : {text!r}")
        if spoken != text:
            print(f"  after table : {spoken!r}")
        torch_ps = misaki_of(spoken)
        onnx_ps = onnx.tokenizer.phonemize(spoken, lang="en-us")
        print(f"  misaki      : {torch_ps}")
        print(f"  espeak      : {onnx_ps}")
        left = without_marks(torch_ps).split()
        right = without_marks(to_misaki_convention(onnx_ps)).split()
        if left == right:
            print("  -> identical once espeak IPA is rewritten misaki's way")
            continue
        print("  -> genuine differences (misaki < | > espeak):")
        for line in difflib.unified_diff(left, right, lineterm="", n=0):
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                print(f"       {line}")
    return 0


# --- installed size attribution ----------------------------------------------

TORCH_ROOTS = ("kokoro", "sounddevice", "scipy")
ONNX_ROOTS = ("kokoro-onnx", "onnxruntime", "phonemizer", "espeakng-loader")


def closure(roots: tuple[str, ...]) -> set[str]:
    """Transitive requirement closure, ignoring unsatisfied extras markers."""
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop().lower().replace("_", "-")
        if name in seen:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        seen.add(name)
        for raw in dist.requires or []:
            # Skip requirements guarded by an extra: they are not installed
            # unless that extra was asked for, and charging them would inflate
            # the figure. Environment markers are left alone.
            if "extra ==" in raw:
                continue
            requirement = raw.split(";")[0].strip()
            for stop in ("[", "(", "<", ">", "=", "!", "~", " "):
                requirement = requirement.split(stop)[0]
            if requirement:
                queue.append(requirement)
    return seen


def dist_bytes(name: str) -> int:
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return 0
    base = dist.locate_file("")
    total = 0
    for file in dist.files or []:
        try:
            total += (Path(str(base)) / str(file)).stat().st_size
        except OSError:
            continue
    return total


def cmd_sizes(args: argparse.Namespace) -> int:
    torch_set = closure(TORCH_ROOTS)
    onnx_set = closure(ONNX_ROOTS)
    shared = torch_set & onnx_set

    def total(names: set[str]) -> int:
        return sum(dist_bytes(name) for name in names)

    onnx_models = 0
    model_dir = Path.home() / ".cache" / "speakd" / "kokoro-onnx"
    if model_dir.is_dir():
        onnx_models = sum(p.stat().st_size for p in model_dir.iterdir() if p.is_file())

    hub = Path.home() / ".cache" / "huggingface" / "hub"
    torch_models = 0
    for candidate in hub.glob("models--hexgrad--Kokoro-82M"):
        torch_models = sum(p.stat().st_size for p in candidate.rglob("*") if p.is_file())

    json.dump(
        {
            "torch_only": {
                "packages": sorted(torch_set - shared),
                "bytes": total(torch_set - shared),
            },
            "onnx_only": {
                "packages": sorted(onnx_set - shared),
                "bytes": total(onnx_set - shared),
            },
            "shared": {"packages": sorted(shared), "bytes": total(shared)},
            "torch_model_bytes": torch_models,
            "onnx_model_bytes": onnx_models,
        },
        sys.stdout,
        indent=2,
    )
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """Run every measurement, spawning a fresh process for the isolated ones."""
    here = [sys.executable, str(Path(__file__).resolve())]
    results: dict[str, Any] = {"isolated": {}}
    for engine_name in ENGINES:
        done = subprocess.run(
            [*here, "isolated", engine_name], capture_output=True, text=True, check=True
        )
        results["isolated"][engine_name] = json.loads(done.stdout)
    for name, argv in (("paired", ["paired", "--rounds", str(args.rounds)]), ("sizes", ["sizes"])):
        done = subprocess.run([*here, *argv], capture_output=True, text=True, check=True)
        results[name] = json.loads(done.stdout)
    json.dump(results, sys.stdout, indent=2)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_engines", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    isolated = sub.add_parser("isolated", help="one engine, fresh process")
    isolated.add_argument("engine", choices=ENGINES)
    isolated.set_defaults(func=cmd_isolated)

    paired = sub.add_parser("paired", help="both engines, one process")
    paired.add_argument("--rounds", type=int, default=3)
    paired.set_defaults(func=cmd_paired)

    wavs = sub.add_parser("wavs", help="write comparison WAV files")
    wavs.add_argument("--out", required=True)
    wavs.set_defaults(func=cmd_wavs)

    phonemes = sub.add_parser("phonemes", help="what each engine would say")
    phonemes.set_defaults(func=cmd_phonemes)

    sizes = sub.add_parser("sizes", help="installed size attribution")
    sizes.set_defaults(func=cmd_sizes)

    every = sub.add_parser("all", help="every measurement")
    every.add_argument("--rounds", type=int, default=3)
    every.set_defaults(func=cmd_all)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
