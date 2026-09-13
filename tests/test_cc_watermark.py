"""Tests for the per-session transcript watermark."""

import os
import threading
from pathlib import Path

from speakd.clients.claude_code.watermark import Watermark, load, locked, save, state_dir


def test_state_dir_honours_the_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    assert state_dir() == tmp_path / "claude-code"


def test_state_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPEAKD_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "speakd" / "claude-code"


def test_an_unknown_session_has_no_watermark(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    assert load("never-seen") is None


def test_a_saved_watermark_round_trips(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    assert load("s1") == Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9")


def test_a_corrupt_watermark_reads_as_absent(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    written = next(p for p in (tmp_path / "claude-code").iterdir() if p.suffix == ".json")
    written.write_text("{ not json", encoding="utf-8")
    assert load("s1") is None


def test_a_session_id_with_a_slash_cannot_escape_the_state_dir(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("../../escaped", Watermark(path="/tmp/t.jsonl", offset=1, uuid="u"))
    assert load("../../escaped") == Watermark(path="/tmp/t.jsonl", offset=1, uuid="u")
    for written in (tmp_path / "claude-code").iterdir():
        assert written.parent == tmp_path / "claude-code"


def test_the_lock_serialises_two_holders(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first() -> None:
        with locked("s1"):
            order.append("first-in")
            inside.set()
            release.wait(timeout=5.0)
            order.append("first-out")

    def second() -> None:
        inside.wait(timeout=5.0)
        with locked("s1"):
            order.append("second-in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    inside.wait(timeout=5.0)
    # The second holder must still be waiting: nothing of its own has run.
    assert order == ["first-in"]
    release.set()
    for thread in threads:
        thread.join(timeout=5.0)
    assert order == ["first-in", "first-out", "second-in"]


def test_saving_replaces_rather_than_truncating(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=1, uuid="a"))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=2, uuid="b"))
    files = sorted(p.name for p in (tmp_path / "claude-code").iterdir())
    assert [name for name in files if name.endswith(".tmp")] == []
    assert load("s1") == Watermark(path="/tmp/t.jsonl", offset=2, uuid="b")
    assert os.access(tmp_path / "claude-code", os.W_OK)


def test_the_lock_actually_blocks_a_second_holder(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Added beyond the brief: `test_the_lock_serialises_two_holders` passes
    with the `flock` call deleted outright -- the main thread wins its assert
    before the unblocked second holder is scheduled. This one waits on the
    second holder instead of racing it, so a missing or shared lock fails.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    entered: list[str] = []
    first_in = threading.Event()
    second_trying = threading.Event()
    second_in = threading.Event()
    release = threading.Event()

    def first() -> None:
        with locked("s1"):
            entered.append("first")
            first_in.set()
            release.wait(timeout=5.0)

    def second() -> None:
        first_in.wait(timeout=5.0)
        second_trying.set()
        with locked("s1"):
            entered.append("second")
            second_in.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    try:
        assert first_in.wait(timeout=5.0)
        assert second_trying.wait(timeout=5.0)
        # The second holder is inside locked() and must stay there.
        assert second_in.wait(timeout=0.5) is False
        assert entered == ["first"]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5.0)
    assert entered == ["first", "second"]


def test_a_path_shaped_session_id_writes_inside_the_state_dir(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Added beyond the brief: the escape test's loop over the state dir is
    vacuous when the id escapes it -- the directory is then empty and the
    round trip still succeeds through the file written outside. This asserts
    the file landed inside, and that nothing landed at the escape target.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    save("../../escaped", Watermark(path="/tmp/t.jsonl", offset=1, uuid="u"))
    inside = sorted((tmp_path / "state" / "claude-code").iterdir())
    assert [p.suffix for p in inside] == [".json"]
    assert not (tmp_path / "escaped.json").exists()


def test_two_session_ids_that_sanitise_alike_keep_separate_state(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    """Added beyond the brief: sanitising alone collides -- "a/b" and "a_b"
    both become "a_b". Sharing one state file between two live sessions both
    drops speech and repeats it, which is the whole property of this layer.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("a/b", Watermark(path="/tmp/t.jsonl", offset=1, uuid="a"))
    save("a_b", Watermark(path="/tmp/t.jsonl", offset=2, uuid="b"))
    assert load("a/b") == Watermark(path="/tmp/t.jsonl", offset=1, uuid="a")
    assert load("a_b") == Watermark(path="/tmp/t.jsonl", offset=2, uuid="b")


def test_a_state_file_that_is_json_but_not_an_object_reads_as_absent(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    """Added beyond the brief: parsing to a list must not raise on `.get`."""
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    written = next(p for p in (tmp_path / "claude-code").iterdir() if p.suffix == ".json")
    written.write_text("[1, 2, 3]", encoding="utf-8")
    assert load("s1") is None


def test_a_state_file_with_a_wrong_typed_offset_reads_as_absent(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    """Added beyond the brief: a null offset from an older or truncated
    writer must read as absent. Handing `None` on as an offset crashes the
    reader's bounds check instead, which is silence rather than a re-read.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    written = next(p for p in (tmp_path / "claude-code").iterdir() if p.suffix == ".json")
    written.write_text('{"path": "/tmp/t.jsonl", "offset": null, "uuid": "u9"}', encoding="utf-8")
    assert load("s1") is None


def test_a_very_long_session_id_still_round_trips(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Added beyond the brief: an unbounded id makes a filename the kernel
    refuses, and the OSError surfaces from `save` inside a hook.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    session_id = "s" * 500
    save(session_id, Watermark(path="/tmp/t.jsonl", offset=7, uuid="u"))
    assert load(session_id) == Watermark(path="/tmp/t.jsonl", offset=7, uuid="u")


def test_the_lock_is_not_lost_when_save_replaces_the_state_file(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    """Added beyond the brief: nothing tested why the lock file is separate.

    `flock` follows the open descriptor, so a lock taken on the state file
    is dropped the moment `save` replaces that inode -- a second hook then
    opens the new file, locks it unopposed, and both speak the same text.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    entered: list[str] = []
    saved = threading.Event()
    second_trying = threading.Event()
    second_in = threading.Event()
    release = threading.Event()

    def first() -> None:
        with locked("s1"):
            save("s1", Watermark(path="/tmp/t.jsonl", offset=1, uuid="a"))
            saved.set()
            release.wait(timeout=5.0)
            entered.append("first-out")

    def second() -> None:
        saved.wait(timeout=5.0)
        second_trying.set()
        with locked("s1"):
            entered.append("second-in")
            second_in.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    try:
        assert saved.wait(timeout=5.0)
        assert second_trying.wait(timeout=5.0)
        assert second_in.wait(timeout=0.5) is False
        assert entered == []
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5.0)
    assert entered == ["first-out", "second-in"]
