"""Tests for reading Claude Code transcript records."""

import json

from speakd.clients.claude_code.transcript import Record, ai_title, parse, speakable


def line(**fields: object) -> bytes:
    record: dict[str, object] = {
        "type": "assistant",
        "uuid": "u1",
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi."}]},
    }
    record.update(fields)
    return (json.dumps(record) + "\n").encode("utf-8")


def test_parse_reads_whole_lines_and_reports_bytes_consumed() -> None:
    chunk = line(uuid="a") + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a", "b"]
    assert consumed == len(chunk)


def test_a_trailing_partial_line_is_not_consumed() -> None:
    whole = line(uuid="a")
    chunk = whole + b'{"type":"assistant","uuid":"b"'
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a"]
    assert consumed == len(whole)


def test_an_unparseable_whole_line_is_skipped_but_consumed() -> None:
    bad = b"not json at all\n"
    chunk = bad + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["b"]
    assert consumed == len(chunk)


def test_a_line_that_is_json_but_not_an_object_is_skipped_but_consumed() -> None:
    """Added beyond the brief: a corrupt line that still parses as JSON must
    not crash the read. An AttributeError here silences every later sentence
    in the session, which is the failure the skip-and-count rule exists for.
    """
    chunk = b"[1, 2, 3]\n" + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["b"]
    assert consumed == len(chunk)


def test_a_record_with_no_uuid_is_skipped_but_consumed() -> None:
    """Added beyond the brief: Claude Code writes summary records that carry
    `leafUuid` and no `uuid`. A Record with uuid None would become the saved
    watermark's uuid, and a watermark that fails to load re-speaks the turn.
    """
    summary = json.dumps({"type": "summary", "summary": "A title", "leafUuid": "x"}) + "\n"
    chunk = summary.encode("utf-8") + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["b"]
    assert consumed == len(chunk)


def test_an_assistant_record_with_no_message_carries_no_text() -> None:
    """Added beyond the brief: a truncated record reaching `_text_of` as None
    must return empty rather than raise -- same crash-the-hook hazard.
    """
    records, _ = parse(line(uuid="a", message=None))
    assert [(r.uuid, r.text) for r in records] == [("a", "")]


def test_an_assistant_message_with_no_content_list_carries_no_text() -> None:
    """Added beyond the brief: the last uncovered guard in `_text_of`.
    Iterating a missing content list raises, and a raising parse is silence.
    """
    records, _ = parse(line(uuid="a", message={"role": "assistant"}))
    assert [(r.uuid, r.text) for r in records] == [("a", "")]


def corrupt_line_with_a_carriage_return() -> bytes:
    """A complete line, terminated by \n, carrying a raw CR inside it.

    JSON forbids unescaped control characters, so only a corrupt line can
    look like this -- which is exactly the case the skip-and-count rule
    promises to survive.
    """
    return b'{"type":"assistant","uuid":"bad","text":"half\rwritten"}\n'


def test_a_carriage_return_inside_a_corrupt_line_does_not_stall_the_read() -> None:
    """`bytes.splitlines` breaks on \r as well as \n, so a stray CR splits one
    corrupt line into a fragment that is not \n-terminated. Treating that as
    the trailing partial line stops the read there and discards the rest of
    the chunk *unconsumed* -- the offset never moves past the stray byte and
    the session goes silent for good, rather than losing one sentence.
    """
    chunk = line(uuid="a") + corrupt_line_with_a_carriage_return() + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a", "b"]
    assert consumed == len(chunk)


def test_reading_forward_from_the_offset_makes_progress_past_a_carriage_return() -> None:
    """The property the offset exists for, stated as a drain: repeated reads
    from the returned offset must reach the end of the chunk.
    """
    chunk = line(uuid="a") + corrupt_line_with_a_carriage_return() + line(uuid="b")
    offset = 0
    seen: list[str] = []
    for _ in range(5):
        records, consumed = parse(chunk[offset:])
        seen += [r.uuid for r in records]
        if consumed == 0:
            break
        offset += consumed
    assert offset == len(chunk)
    assert seen == ["a", "b"]


def test_a_transcript_written_with_crlf_endings_still_parses() -> None:
    """The other side of splitting strictly on \n: a CR left at the end of a
    line is JSON whitespace, so CRLF transcripts must keep working.
    """
    chunk = line(uuid="a").replace(b"\n", b"\r\n") + line(uuid="b").replace(b"\n", b"\r\n")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a", "b"]
    assert consumed == len(chunk)


def test_user_records_are_kept_but_carry_no_text() -> None:
    chunk = line(type="user", uuid="u", message={"role": "user", "content": "hello"})
    records, _ = parse(chunk)
    assert [(r.kind, r.text) for r in records] == [("user", "")]


def test_sidechain_records_are_marked() -> None:
    records, _ = parse(line(uuid="s", isSidechain=True))
    assert records[0].is_sidechain is True


def test_a_record_with_no_isSidechain_key_is_treated_as_main_thread() -> None:
    """Absence means False here, and False means spoken.

    Every record Claude Code writes carries the flag, so this pins the
    assumption rather than a behaviour: if the field is ever renamed or
    dropped, the suite says so instead of the user hearing a subagent.
    """
    record = {
        "type": "assistant",
        "uuid": "a",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi."}]},
    }
    records, _ = parse((json.dumps(record) + "\n").encode("utf-8"))
    assert [(r.uuid, r.is_sidechain) for r in records] == [("a", False)]
    assert speakable(records) == "Hi."


def test_a_record_with_no_type_is_skipped_but_consumed() -> None:
    """The other half of the same assumption. A record with no `type` cannot
    be classified, so it is dropped -- but the line it occupied is still
    counted, or the offset would never move past it.
    """
    typeless = json.dumps({"uuid": "x", "isSidechain": False, "message": {}}) + "\n"
    chunk = typeless.encode("utf-8") + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["b"]
    assert consumed == len(chunk)


def test_thinking_and_tool_use_blocks_are_not_text() -> None:
    content = [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "The answer is four."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "The answer is four."


def test_empty_and_unstringy_text_blocks_leave_no_gap() -> None:
    """Added beyond the brief: Claude Code emits `{"type":"text","text":""}`
    blocks on a turn that is only tool use. Joining them blindly leaves a
    leading blank paragraph, and a non-string text crashes the join -- which
    silences the hook entirely rather than losing one sentence.
    """
    content = [
        {"type": "text", "text": ""},
        {"type": "text", "text": "The answer."},
        {"type": "text", "text": None},
    ]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "The answer."


def test_a_non_text_block_that_carries_a_text_field_is_not_spoken() -> None:
    """The block type decides, not the presence of a `text` key.

    Added beyond the brief: with a `thinking`/`tool_use` fixture alone the
    `type == "text"` check survives deletion, because neither block has a
    `text` field to be picked up by accident.
    """
    content = [
        {"type": "tool_result", "text": "Command output nobody asked to hear."},
        {"type": "text", "text": "Done."},
    ]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "Done."


def test_several_text_blocks_in_one_record_join_as_paragraphs() -> None:
    content = [{"type": "text", "text": "One."}, {"type": "text", "text": "Two."}]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "One.\n\nTwo."


def test_speakable_keeps_only_assistant_text_and_drops_sidechains() -> None:
    records = [
        Record(uuid="a", kind="assistant", is_sidechain=False, text="Spoken."),
        Record(uuid="b", kind="assistant", is_sidechain=True, text="Subagent chatter."),
        Record(uuid="c", kind="user", is_sidechain=False, text=""),
        Record(uuid="d", kind="assistant", is_sidechain=False, text="Also spoken."),
    ]
    assert speakable(records) == "Spoken.\n\nAlso spoken."


def test_a_user_record_carrying_text_is_still_not_spoken() -> None:
    """Added beyond the brief: `speakable` is public and takes any records,
    so the assistant-kind filter must hold even for a record that has text.
    Reading the user's own words back to them is the failure guarded here.
    """
    records = [
        Record(uuid="u", kind="user", is_sidechain=False, text="My own words."),
        Record(uuid="a", kind="assistant", is_sidechain=False, text="The reply."),
    ]
    assert speakable(records) == "The reply."


def test_speakable_of_nothing_is_empty() -> None:
    assert speakable([]) == ""


def test_an_ai_title_record_yields_the_session_name() -> None:
    line = b'{"type": "ai-title", "aiTitle": "Claude code feature enablement", "sessionId": "x"}\n'
    records, _ = parse(line)
    assert ai_title(records) == "Claude code feature enablement"


def test_the_latest_title_wins() -> None:
    chunk = (
        b'{"type": "ai-title", "aiTitle": "First guess", "sessionId": "x"}\n'
        b'{"type": "ai-title", "aiTitle": "Better name", "sessionId": "x"}\n'
    )
    records, _ = parse(chunk)
    assert ai_title(records) == "Better name"


def test_no_title_record_yields_none() -> None:
    records, _ = parse(b'{"type": "assistant", "uuid": "u", "message": {"content": []}}\n')
    assert ai_title(records) is None
