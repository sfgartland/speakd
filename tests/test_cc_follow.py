"""Tests for the loop that speaks a transcript as it grows."""

import json
from pathlib import Path
from typing import Any

from speakd.clients import registry
from speakd.clients.claude_code.follow import Follower


def assistant(uuid: str, text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "isSidechain": False,
                "message": {"content": [{"type": "text", "text": text}]},
            }
        ).encode()
        + b"\n"
    )


class Spy:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, verb: str, source: str, payload: dict[str, Any]) -> None:
        self.sent.append((verb, source, payload))

    def spoken(self) -> list[str]:
        return [p["text"] for v, _s, p in self.sent if v == "enqueue"]


def follower(tmp_path, monkeypatch, spy) -> tuple[Follower, Path]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"")
    registry.register("s1", transcript, "/home/me/My Project", client="claude-code")
    return Follower(send=spy), transcript


def test_new_text_is_spoken(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()  # registers at end of file; nothing to say yet
    with transcript.open("ab") as handle:
        handle.write(assistant("u1", "Hello there."))
    f.tick()
    assert spy.spoken() == ["Hello there."]


def test_text_is_never_spoken_twice(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    with transcript.open("ab") as handle:
        handle.write(assistant("u1", "Once."))
    f.tick()
    f.tick()
    assert spy.spoken() == ["Once."]


def test_a_partial_line_waits_for_the_rest(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    whole = assistant("u1", "Complete.")
    with transcript.open("ab") as handle:
        handle.write(whole[:20])
    f.tick()
    assert spy.spoken() == []
    with transcript.open("ab") as handle:
        handle.write(whole[20:])
    f.tick()
    assert spy.spoken() == ["Complete."]


def test_a_new_session_starts_at_end_of_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Registering must not read an hour of history aloud.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(assistant("old", "Ancient history."))
    registry.register("s1", transcript, "/p", client="claude-code")
    spy = Spy()
    f = Follower(send=spy)
    f.tick()
    assert spy.spoken() == []


def test_the_channel_is_labelled_from_the_working_directory(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, _transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    labels = [p["label"] for v, _s, p in spy.sent if v == "set_label"]
    assert labels == ["Claude Code · My Project"]


def test_an_ai_title_renames_the_channel(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    with transcript.open("ab") as handle:
        handle.write(
            json.dumps({"type": "ai-title", "aiTitle": "Real name", "sessionId": "s1"}).encode()
            + b"\n"
        )
    f.tick()
    labels = [p["label"] for v, _s, p in spy.sent if v == "set_label"]
    assert labels[-1] == "Claude Code · Real name"


def test_the_label_is_not_resent_when_it_has_not_changed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    # New text, so the second tick reaches the relabel at all -- an idle tick
    # returns before it, and would pass this test with no dedup in place.
    with transcript.open("ab") as handle:
        handle.write(assistant("u1", "Something."))
    f.tick()
    labels = [p for v, _s, p in spy.sent if v == "set_label"]
    assert len(labels) == 1


def test_a_sidechain_message_is_not_spoken(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    record = json.loads(assistant("u1", "Subagent output."))
    record["isSidechain"] = True
    with transcript.open("ab") as handle:
        handle.write(json.dumps(record).encode() + b"\n")
    f.tick()
    assert spy.spoken() == []


def test_a_vanished_transcript_does_not_stop_the_loop(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    transcript.unlink()
    f.tick()  # must not raise
