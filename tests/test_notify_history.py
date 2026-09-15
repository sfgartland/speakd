"""The record of what arrived is the only way to answer "why was that silent?"."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from speakd.clients.notifications import history


def _entry(app: str = "Google Chrome", *, spoken: bool = True, when: float = 1.0) -> history.Entry:
    return history.Entry(
        when=when,
        app=app,
        summary="Mamma",
        body="are we still on for tomorrow?",
        spoken=spoken,
        rule="chrome" if spoken else "",
        reason="" if spoken else "no rule matched",
    )


def test_a_recorded_notification_comes_back() -> None:
    history.record(_entry())
    [got] = history.recent()
    assert got == _entry()


def test_entries_come_back_oldest_first() -> None:
    for index in range(3):
        history.record(_entry(app=f"app-{index}", when=float(index)))
    assert [e.app for e in history.recent()] == ["app-0", "app-1", "app-2"]


def test_the_history_stops_at_its_cap() -> None:
    """Rewritten whole on every notification, so an uncapped file is a growing write."""
    for index in range(history.HISTORY_LIMIT + 25):
        history.record(_entry(app=f"app-{index}", when=float(index)))
    entries = history.recent()
    assert len(entries) == history.HISTORY_LIMIT
    # The newest survive, not the oldest: this file exists to answer a
    # question about a notification that has just happened.
    assert entries[-1].app == f"app-{history.HISTORY_LIMIT + 24}"


def test_a_limit_smaller_than_the_file_returns_the_newest() -> None:
    for index in range(10):
        history.record(_entry(app=f"app-{index}", when=float(index)))
    assert [e.app for e in history.recent(limit=3)] == ["app-7", "app-8", "app-9"]


def test_recent_on_a_file_that_was_never_written_is_empty() -> None:
    assert history.recent() == []


def test_clear_forgets_everything() -> None:
    history.record(_entry())
    history.clear()
    assert history.recent() == []


def test_clearing_a_history_that_does_not_exist_is_not_an_error() -> None:
    history.clear()


def test_one_corrupt_line_does_not_lose_the_rest() -> None:
    history.record(_entry(app="before", when=1.0))
    with history.path().open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    history.record(_entry(app="after", when=2.0))
    assert [e.app for e in history.recent()] == ["before", "after"]


def test_a_line_that_is_json_but_not_an_object_is_skipped() -> None:
    history.state_dir().mkdir(parents=True, exist_ok=True)
    history.path().write_text('["not", "an", "object"]\n', encoding="utf-8")
    assert history.recent() == []


def test_a_missing_field_falls_back_rather_than_losing_the_entry() -> None:
    history.state_dir().mkdir(parents=True, exist_ok=True)
    history.path().write_text(json.dumps({"app": "Partial"}) + "\n", encoding="utf-8")
    [got] = history.recent()
    assert got.app == "Partial"
    assert got.spoken is False
    assert got.when == 0.0


def test_recording_into_an_unwritable_directory_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The record is the diagnostic, never the product: it must not cost speech."""
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(blocked))
    history.record(_entry())
    assert history.recent() == []


def test_no_partial_file_is_left_where_a_reader_could_see_it() -> None:
    """Written whole through a rename, so the cap is never briefly untrue."""
    history.record(_entry())
    leftovers = list(history.state_dir().glob("*.tmp"))
    assert leftovers == []
