"""Tests for the flags that outlive a daemon."""

import json
import pathlib

from speakd.state import DaemonState, load, save, state_path


def test_state_path_follows_xdg(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert state_path() == __import__("pathlib").Path("/tmp/xdg/speakd/state.json")


def test_a_missing_file_is_the_default_state(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load() == DaemonState(muted=False, disabled=False)


def test_what_is_saved_is_what_loads(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save(DaemonState(muted=True, disabled=True))
    assert load() == DaemonState(muted=True, disabled=True)


def test_unreadable_state_is_the_default_rather_than_a_crash(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A daemon that will not start because a state file got truncated is a
    # worse failure than one that starts audible.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert load() == DaemonState(muted=False, disabled=False)


def test_unknown_keys_are_ignored(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"muted": True, "from_a_later_version": 1}))
    assert load() == DaemonState(muted=True, disabled=False)


def test_the_suite_never_reads_the_developers_real_state() -> None:
    """The isolation in conftest is load-bearing, so it gets an assertion.

    Without it, every file that builds a `Daemon` without ever naming
    `speakd.state` -- and there are five -- reads the machine's real mute
    flag. One genuine `speakctl mute` would then make unrelated suites fail
    for a reason nowhere in their own source.
    """
    assert pathlib.Path.home() not in state_path().parents


def test_speed_round_trips_and_defaults() -> None:
    from dataclasses import replace

    from speakd import state

    assert state.load().speed == 1.0
    state.save(replace(state.load(), speed=1.3))
    assert state.load().speed == 1.3
    assert state.load().muted is False


def test_a_nonsense_speed_reads_as_the_default() -> None:
    from speakd import state

    state.state_path().parent.mkdir(parents=True, exist_ok=True)
    state.state_path().write_text('{"speed": "fast"}', encoding="utf-8")
    assert state.load().speed == 1.0
