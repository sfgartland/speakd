"""Tests for extracting the text a session has not spoken yet."""

import json
from pathlib import Path

from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.watermark import Watermark


def assistant(uuid: str, text: str, *, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "isSidechain": sidechain,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def user(uuid: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "isSidechain": False,
            "message": {"role": "user", "content": "go on"},
        }
    )


def tool_result(uuid: str) -> str:
    """A tool result, which Claude Code writes as a record of type "user"."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        }
    )


def write(path: Path, *lines: str) -> None:
    # newline="" so the fixture is not newline-translated on write: these
    # tests assert byte offsets, and a writer that turned "\n" into os.linesep
    # would shift every one of them by a byte per line.
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="")


def test_without_a_watermark_everything_readable_is_spoken(tmp_path: Path) -> None:
    """No watermark means "start here", not "guess where this turn began".

    The guess was a backward scan for the last record of type "user", and it
    was wrong for the reason test_a_tool_result_is_not_the_turn_boundary
    gives. A caller that must not hear a session's history writes its own
    watermark at end of file first; the follower does exactly that the moment
    a session registers, so nothing reaches here expecting to be guessed for.
    """
    transcript = tmp_path / "t.jsonl"
    write(
        transcript,
        assistant("a1", "Earlier."),
        user("u1"),
        assistant("a2", "The current answer."),
    )
    text, mark = new_text(transcript, None)
    assert text == "Earlier.\n\nThe current answer."
    assert mark is not None and mark.uuid == "a2"


def test_a_tool_result_is_not_the_turn_boundary(tmp_path: Path) -> None:
    """Tool results are records of type "user".

    So scanning back for the last one dropped every prose block before it --
    on any turn that used a tool, most of what was said. After a `/clear` or
    a resume, when there is no watermark to resume from, that was the whole
    answer bar its last paragraph.
    """
    transcript = tmp_path / "t.jsonl"
    write(
        transcript,
        user("u0"),
        assistant("a1", "First."),
        tool_result("u1"),
        assistant("a2", "Second."),
    )
    text, _ = new_text(transcript, None)
    assert "First." in text
    assert "Second." in text


def test_a_watermark_resumes_where_it_stopped(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "First."))
    _, first = new_text(transcript, None)
    assert first is not None

    with transcript.open("a", encoding="utf-8", newline="") as handle:
        handle.write(assistant("a2", "Second.") + "\n")
    text, second = new_text(transcript, first)
    assert text == "Second."
    assert second is not None and second.uuid == "a2"


def test_nothing_new_reads_as_empty_and_no_new_watermark(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "Only this."))
    _, mark = new_text(transcript, None)
    text, again = new_text(transcript, mark)
    assert text == ""
    assert again is None


def test_a_half_written_line_waits_for_its_newline(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "Complete."))
    with transcript.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"type":"assistant","uuid":"a2"')
    text, mark = new_text(transcript, None)
    assert text == "Complete."
    assert mark is not None and mark.offset == len(assistant("a1", "Complete.")) + 1


def test_a_watermark_for_another_transcript_is_ignored(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "Fresh."))
    stale = Watermark(path=str(tmp_path / "other.jsonl"), offset=999, uuid="x")
    text, mark = new_text(transcript, stale)
    assert text == "Fresh."
    assert mark is not None and mark.path == str(transcript)


def test_an_offset_past_the_end_rescans(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "Short file now."))
    stale = Watermark(path=str(transcript), offset=10_000, uuid="x")
    text, _ = new_text(transcript, stale)
    assert text == "Short file now."


def test_sidechain_records_advance_the_watermark_without_speaking(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("s1", "Subagent noise.", sidechain=True))
    text, mark = new_text(transcript, None)
    assert text == ""
    assert mark is not None and mark.uuid == "s1"


def test_a_missing_transcript_is_not_an_error(tmp_path: Path) -> None:
    text, mark = new_text(tmp_path / "absent.jsonl", None)
    assert text == ""
    assert mark is None


def test_a_resumed_read_counts_its_offset_from_where_it_resumed(tmp_path: Path) -> None:
    """Added beyond the brief: nothing asserted the resumed offset itself, so
    `offset=consumed` (counting from zero) passed. That offset lands mid-file
    and the next hook re-speaks everything after it -- spoken twice, which is
    the one property this layer exists to guarantee.
    """
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "First."))
    _, first = new_text(transcript, None)
    assert first is not None

    with transcript.open("a", encoding="utf-8", newline="") as handle:
        handle.write(assistant("a2", "Second.") + "\n")
    text, second = new_text(transcript, first)
    assert text == "Second."
    assert second is not None and second.offset == transcript.stat().st_size

    assert new_text(transcript, second) == ("", None)


def test_the_watermark_names_the_last_record_of_the_window(tmp_path: Path) -> None:
    """Added beyond the brief: every fixture window held one record, so
    `records[0]` passed. A watermark naming the first record of the window
    misreports where the read stopped to anything reading it later.
    """
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "One."), assistant("a2", "Two."))
    text, mark = new_text(transcript, None)
    assert text == "One.\n\nTwo."
    assert mark is not None and mark.uuid == "a2"


def test_another_transcripts_watermark_is_ignored_even_when_its_offset_fits(
    tmp_path: Path,
) -> None:
    """Added beyond the brief: the brief's stale mark has offset 999 on a
    shorter file, so the bounds check alone rejects it and the path check
    survives deletion. A resumed offset from *another* transcript points at
    an arbitrary byte of this one, and everything before it is never spoken.
    """
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "One."), assistant("a2", "Two."))
    first_two_lines = len(user("u1")) + len(assistant("a1", "One.")) + 2
    stale = Watermark(path=str(tmp_path / "other.jsonl"), offset=first_two_lines, uuid="x")
    text, mark = new_text(transcript, stale)
    assert text == "One.\n\nTwo."
    assert mark is not None and mark.path == str(transcript)


def test_a_negative_offset_is_not_trusted(tmp_path: Path) -> None:
    """Added beyond the brief: `seek` to a negative offset raises, the read
    returns nothing, and no watermark is written -- so the session stays
    silent for good. A stored offset we cannot use means rescan, not silence.
    """
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "Still speakable."))
    stale = Watermark(path=str(transcript), offset=-1, uuid="x")
    text, mark = new_text(transcript, stale)
    assert text == "Still speakable."
    assert mark is not None and mark.offset == transcript.stat().st_size


def test_a_transcript_that_cannot_be_opened_is_not_an_error(tmp_path: Path) -> None:
    """Added beyond the brief: a path that stats but will not open (here a
    directory) must not raise out of a hook.
    """
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    assert new_text(directory, None) == ("", None)
