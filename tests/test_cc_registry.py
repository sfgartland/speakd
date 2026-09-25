"""Tests for which Claude Code sessions are worth following."""

import time
from pathlib import Path

from speakd.clients import registry


def test_a_registered_session_is_live(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/home/me/project", client="claude-code")
    live = registry.live()
    assert [r.session_id for r in live] == ["s1"]
    assert live[0].transcript == Path("/tmp/a.jsonl")
    assert live[0].cwd == "/home/me/project"


def test_registering_again_refreshes_rather_than_duplicates(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    registry.register("s1", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    assert len(registry.live()) == 1


def test_a_stale_session_drops_out(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    assert registry.live(max_idle_seconds=-1.0) == []


def test_an_unreadable_registration_is_skipped_not_fatal(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # One corrupt file must not stop every other session being spoken.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("good", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    bad = registry._registration_path("claude-code", "bad")
    bad.write_text("{not json")
    assert [r.session_id for r in registry.live()] == ["good"]


def test_touched_is_recent(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    assert time.time() - registry.live()[0].touched < 5.0


def test_a_session_id_with_a_slash_cannot_escape_the_state_dir(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The watermark beside it has been sanitised since it was written, and
    # `register` swallows OSError -- so an unsanitised path here would fail
    # silently rather than loudly. Mirrors the watermark test of the same name.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("../../escaped", Path("/tmp/a.jsonl"), "/p", client="claude-code")
    directory = tmp_path / "claude-code"
    for written in directory.iterdir():
        assert written.parent == directory
    assert [r.session_id for r in registry.live()] == ["../../escaped"]
