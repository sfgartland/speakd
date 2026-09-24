"""Tests for the speakctl command line."""

import json

import pytest

from speakd.cli import main
from speakd.model import Piece


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Keep the default profiles-file lookup off the real machine's home
    directory, so every test here is pure regardless of what a developer's
    or CI runner's `~/.config/speakd/profiles.toml` happens to contain."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_dry_run_prints_a_timeline(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "One. Two.", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [s["text"] for s in payload["segments"]] == ["One.", "Two."]
    assert payload["segments"][0]["audio_offset"] == 0.0
    assert payload["duration"] > 0.0


def test_dry_run_reads_stdin_when_no_text_given(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("Hello there."))
    assert main(["say", "--dry-run"]) == 0
    assert "Hello there." in capsys.readouterr().out


def test_empty_input_is_not_an_error(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["say", "   ", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["segments"] == []


def test_unknown_subcommand_exits_nonzero() -> None:
    assert main([]) == 2


def test_max_chars_below_one_is_rejected_without_hanging(capsys) -> None:  # type: ignore[no-untyped-def]
    """`speakd.segmenter._split_words` loops forever when max_chars < 1: with
    max_chars == 0 the computed end index equals the start index, so a
    non-space character never advances the loop.

    Check the parse-and-validate path directly first — `_validate_say_args`
    touches no segmenter or engine code, so a regression that removes this
    guard fails this assertion immediately instead of hanging the suite.
    Only after that do we exercise `main()` itself, which is safe to call
    here precisely because the guard runs before any code that could loop.
    """
    from speakd.cli import _build_parser, _validate_say_args

    args = _build_parser().parse_args(["say", "x", "--max-chars", "0", "--dry-run"])
    assert _validate_say_args(args) is not None

    code = main(["say", "x", "--max-chars", "0", "--dry-run"])
    assert code != 0
    assert "max-chars" in capsys.readouterr().err


def test_negative_max_chars_is_rejected(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "x", "--max-chars", "-5", "--dry-run"])
    assert code != 0
    assert "max-chars" in capsys.readouterr().err


def test_speed_of_zero_is_rejected(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "x", "--speed", "0", "--dry-run"])
    assert code != 0
    assert "speed" in capsys.readouterr().err


def test_negative_speed_is_rejected(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "x", "--speed", "-1", "--dry-run"])
    assert code != 0
    assert "speed" in capsys.readouterr().err


def test_say_exits_nonzero_when_a_segment_failed(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A partial failure must be distinguishable from a clean run."""
    from speakd import cli
    from speakd.pipeline import SpeechResult
    from speakd.timeline import Timeline

    def failing_speak(*args: object, **kwargs: object) -> SpeechResult:
        return SpeechResult(timeline=Timeline(), errors=["'One.': engine exploded"])

    monkeypatch.setattr(cli, "speak", failing_speak)

    assert cli.main(["say", "One. Two.", "--dry-run"]) == 1
    assert "engine exploded" in capsys.readouterr().err


def test_say_exits_zero_when_nothing_failed() -> None:
    assert main(["say", "One. Two.", "--dry-run"]) == 0


def test_missing_kokoro_extra_is_reported_not_traced(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Constructing KokoroEngine is what imports kokoro; without the extra
    that raised ModuleNotFoundError straight out of main()."""
    from speakd.synth import kokoro_engine

    def missing(*args: object, **kwargs: object) -> object:
        raise ModuleNotFoundError("No module named 'kokoro'")

    monkeypatch.setattr(kokoro_engine, "KokoroEngine", missing)

    # Not a dry run, but it returns before touching an audio device.
    code = main(["say", "One."])
    assert code != 0
    assert "kokoro extra is not installed" in capsys.readouterr().err


def test_dry_run_applies_default_profile_transforms(capsys) -> None:  # type: ignore[no-untyped-def]
    """The headline case: markdown rendering plus the pronunciation table are
    on by default, reachable from the command line with no extra flags."""
    code = main(["say", "The API returned null.", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    spoken = " ".join(s["text"] for s in payload["segments"])
    assert spoken == "The A P I returned null."


def test_no_transforms_preserves_raw_text(capsys) -> None:  # type: ignore[no-untyped-def]
    """`--no-transforms` keeps today's behaviour reachable for debugging."""
    code = main(["say", "The API returned null.", "--dry-run", "--no-transforms"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [s["text"] for s in payload["segments"]] == ["The API returned null."]


def test_unknown_profile_is_a_clear_error(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "hello", "--dry-run", "--profile", "nonexistent"])
    assert code != 0
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "Traceback" not in err


def test_missing_transform_warns_still_speaks_and_exits_nonzero(capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A silently-dropped chunk of the chain must be visible in the exit code,
    not only on stderr -- an agent driving this over a shell command checks
    the former, and today it cannot tell this apart from a clean run."""
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.p]\ntransforms = ["markdown", "citations"]\n')
    code = main(
        [
            "say",
            "The API returned null.",
            "--dry-run",
            "--profile",
            "p",
            "--profiles-file",
            str(path),
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "citations" in captured.err
    assert "p" in captured.err
    payload = json.loads(captured.out)
    assert payload["segments"], "the utterance must still be spoken"
    assert payload["missing_transforms"] == ["citations"]
    assert payload["transform_errors"] == []
    assert payload["errors"] == []


def test_a_raising_transform_is_reported_and_exit_is_nonzero(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Same rule as a missing transform: the chain loses that transform's
    contribution, never the utterance, but the exit code must show it."""
    from speakd.plugins import builtin

    def boom(pieces: object) -> object:
        raise RuntimeError("transform exploded")

    monkeypatch.setattr(builtin, "markdown", boom)

    code = main(["say", "The API returned null.", "--dry-run"])
    assert code == 1
    captured = capsys.readouterr()
    assert "transform exploded" in captured.err
    payload = json.loads(captured.out)
    assert payload["segments"], "the utterance must still be spoken"
    assert payload["missing_transforms"] == []
    assert len(payload["transform_errors"]) == 1
    assert "transform exploded" in payload["transform_errors"][0]
    assert payload["errors"] == []


def test_dry_run_json_reports_both_a_missing_and_a_failing_transform(  # type: ignore[no-untyped-def]
    capsys, tmp_path, monkeypatch
) -> None:
    """The two reporting surfaces must agree: the --dry-run JSON needs to
    carry the same picture as the exit code, not just speak()'s errors --
    otherwise an agent reading the JSON (the whole point of --dry-run) sees
    "errors": [] on a run that exited 1."""
    from speakd.plugins import builtin

    def boom(pieces: object) -> object:
        raise RuntimeError("transform exploded")

    monkeypatch.setattr(builtin, "markdown", boom)

    path = tmp_path / "profiles.toml"
    path.write_text('[profile.p]\ntransforms = ["citations", "markdown"]\n')

    code = main(
        [
            "say",
            "The API returned null.",
            "--dry-run",
            "--profile",
            "p",
            "--profiles-file",
            str(path),
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing_transforms"] == ["citations"]
    assert len(payload["transform_errors"]) == 1
    assert "transform exploded" in payload["transform_errors"][0]
    assert payload["errors"] == []
    assert payload["segments"], "the utterance must still be spoken"


def test_no_transforms_still_uses_the_profiles_voice_and_speed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`--no-transforms` names the transform chain, not the voice: a profile
    bundling a non-default voice/speed with its chain still supplies them
    when the chain itself is skipped, while the text stays untransformed."""
    from speakd import cli
    from speakd.pipeline import SpeechResult
    from speakd.timeline import Timeline

    path = tmp_path / "profiles.toml"
    path.write_text(
        '[profile.p]\ntransforms = ["markdown", "pronunciation"]\nvoice = "af_bella"\nspeed = 0.8\n'
    )

    captured: dict[str, object] = {}
    captured_pieces: list[Piece] = []

    def fake_speak(pieces, engine, player, **kwargs):  # type: ignore[no-untyped-def]
        captured_pieces.extend(pieces)
        captured.update(kwargs)
        return SpeechResult(timeline=Timeline())

    monkeypatch.setattr(cli, "speak", fake_speak)
    code = main(
        [
            "say",
            "The API returned null.",
            "--dry-run",
            "--no-transforms",
            "--profile",
            "p",
            "--profiles-file",
            str(path),
        ]
    )
    assert code == 0
    assert captured["voice"] == "af_bella"
    assert captured["speed"] == 0.8
    spoken = [p.spoken for p in captured_pieces]
    assert spoken == ["The API returned null."]


def test_no_transforms_with_unknown_profile_still_errors(capsys) -> None:  # type: ignore[no-untyped-def]
    """Profile resolution -- and its exit-2 path -- still runs even when the
    transform chain itself is going to be skipped."""
    code = main(["say", "hi", "--dry-run", "--no-transforms", "--profile", "nonexistent"])
    assert code != 0
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "Traceback" not in err


def test_malformed_profiles_file_exits_cleanly(capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "profiles.toml"
    path.write_text("[profile.bad]\nspeed = 0\n")
    code = main(["say", "hello", "--dry-run", "--profiles-file", str(path)])
    assert code != 0
    err = capsys.readouterr().err
    assert "speed" in err
    assert "Traceback" not in err


def test_profile_voice_and_speed_apply_when_flags_are_omitted(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd import cli
    from speakd.pipeline import SpeechResult
    from speakd.timeline import Timeline

    path = tmp_path / "profiles.toml"
    path.write_text('[profile.p]\ntransforms = []\nvoice = "af_bella"\nspeed = 0.8\n')

    captured: dict[str, object] = {}

    def fake_speak(pieces, engine, player, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SpeechResult(timeline=Timeline())

    monkeypatch.setattr(cli, "speak", fake_speak)
    code = main(["say", "hi", "--dry-run", "--profile", "p", "--profiles-file", str(path)])
    assert code == 0
    assert captured["voice"] == "af_bella"
    assert captured["speed"] == 0.8


def test_cli_voice_and_speed_override_the_profile(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd import cli
    from speakd.pipeline import SpeechResult
    from speakd.timeline import Timeline

    path = tmp_path / "profiles.toml"
    path.write_text('[profile.p]\ntransforms = []\nvoice = "af_bella"\nspeed = 0.8\n')

    captured: dict[str, object] = {}

    def fake_speak(pieces, engine, player, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SpeechResult(timeline=Timeline())

    monkeypatch.setattr(cli, "speak", fake_speak)
    code = main(
        [
            "say",
            "hi",
            "--dry-run",
            "--profile",
            "p",
            "--profiles-file",
            str(path),
            "--voice",
            "af_sky",
            "--speed",
            "1.5",
        ]
    )
    assert code == 0
    assert captured["voice"] == "af_sky"
    assert captured["speed"] == 1.5


def test_default_voice_and_speed_when_nothing_is_specified(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No flags at all must still give af_heart at 1.1 -- today's behaviour."""
    from speakd import cli
    from speakd.pipeline import SpeechResult
    from speakd.timeline import Timeline

    captured: dict[str, object] = {}

    def fake_speak(pieces, engine, player, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SpeechResult(timeline=Timeline())

    monkeypatch.setattr(cli, "speak", fake_speak)
    code = main(["say", "hi", "--dry-run"])
    assert code == 0
    assert captured["voice"] == "af_heart"
    assert captured["speed"] == 1.1


def _fake_call(monkeypatch, answer):  # type: ignore[no-untyped-def]
    from speakd import cli

    sent = []

    def call(socket, request):  # type: ignore[no-untyped-def]
        sent.append(request)
        return answer

    monkeypatch.setattr(cli, "_call", call)
    return sent


def test_speed_sends_set_speed_and_prints_what_was_applied(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response, Verb

    sent = _fake_call(monkeypatch, Response(ok=True, data={"speed": 1.6}))
    assert main(["speed", "3"]) == 0
    assert sent[0].verb is Verb.SET_SPEED
    assert sent[0].payload == {"speed": 3.0}
    assert capsys.readouterr().out.strip() == "1.6"


def test_speed_with_no_value_prints_the_current_one(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response, Verb

    sent = _fake_call(monkeypatch, Response(ok=True, data={"speed": 1.2}))
    assert main(["speed"]) == 0
    assert sent[0].verb is Verb.STATUS
    assert capsys.readouterr().out.strip() == "1.2"


def test_speed_refuses_what_is_not_a_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response

    sent = _fake_call(monkeypatch, Response(ok=True))
    assert main(["speed", "fast"]) != 0
    assert sent == []


def test_seek_sends_by_or_index(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response, Verb

    sent = _fake_call(monkeypatch, Response(ok=True, data={"index": 1}))
    assert main(["seek", "--by", "-1"]) == 0
    assert main(["seek", "--index", "3"]) == 0
    assert [r.verb for r in sent] == [Verb.SEEK, Verb.SEEK]
    assert [r.payload for r in sent] == [{"by": -1}, {"index": 3}]


def test_speed_can_ramp(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response

    sent = _fake_call(monkeypatch, Response(ok=True, data={"speed": 1.4}))
    assert main(["speed", "1.4", "--ramp", "1.5"]) == 0
    assert sent[0].payload == {"speed": 1.4, "ramp": 1.5}
