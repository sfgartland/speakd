"""Tests for the shared session registry both agent clients use."""

import json
import time
from pathlib import Path

from speakd.clients import registry
from speakd.paths import client_state_dir


def test_registrations_land_in_a_directory_per_client(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    registry.register("ses_1", Path(""), "/work", client="opencode", agent_pid=700)
    claude_files = list(client_state_dir("claude-code").glob("*.session.json"))
    opencode_files = list(client_state_dir("opencode").glob("*.session.json"))
    assert len(claude_files) == 1 and len(opencode_files) == 1
    body = json.loads(opencode_files[0].read_text(encoding="utf-8"))
    assert body["client"] == "opencode"
    assert body["agent_pid"] == 700
    assert body["transcript"] == ""
    assert isinstance(body["touched"], float)


def test_live_reads_every_client_and_round_trips(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    registry.register("ses_1", Path(""), "/work", client="opencode", agent_pid=700)
    found = {(r.client, r.session_id, r.agent_pid, r.cwd) for r in registry.live()}
    assert found == {
        ("claude-code", "abc", 200, "/work"),
        ("opencode", "ses_1", 700, "/work"),
    }


def test_an_idle_registration_is_not_live(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    assert registry.live(max_idle_seconds=0.0) == []


def test_old_files_without_a_client_are_skipped(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    directory = registry.state_dir("claude-code")
    directory.mkdir(parents=True)
    (directory / "stale-000000000000.session.json").write_text(
        json.dumps(
            {
                "session_id": "stale",
                "transcript": "/t.jsonl",
                "cwd": "/w",
                "touched": time.time(),
                "claude_pid": 9,
            }
        ),
        encoding="utf-8",
    )
    assert registry.live() == []


def test_a_path_shaped_session_id_stays_in_the_directory(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("../evil", Path(""), "/w", client="opencode")
    assert len(list(registry.state_dir("opencode").glob("*.session.json"))) == 1
