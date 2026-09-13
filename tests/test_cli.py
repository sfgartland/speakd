"""Tests for the speakctl command line."""

import json

from speakd.cli import main


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
