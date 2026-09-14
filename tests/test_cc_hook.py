"""Tests for the hook entry point's dispatch and failure policy."""

import io
import json
import socket as socketlib
import subprocess as sp
import threading
import time
from pathlib import Path

import pytest

from speakd.clients.claude_code import hook
from speakd.clients.claude_code import send as send_module
from speakd.clients.claude_code.watermark import load, state_dir

PLUGIN = Path(__file__).resolve().parent.parent / "clients" / "claude-code"
# Read from the manifest rather than restated, so the two cannot drift.
PROMPT_BUDGET = min(
    entry["timeout"]
    for matcher in json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))[
        "hooks"
    ]["UserPromptSubmit"]
    for entry in matcher["hooks"]
)


def payload(**fields: object) -> str:
    body: dict[str, object] = {
        "session_id": "s1",
        "transcript_path": "/nonexistent/t.jsonl",
        "hook_event_name": "Stop",
        "cwd": "/tmp",
    }
    body.update(fields)
    return json.dumps(body)


def transcript(tmp_path: Path, *texts: str) -> Path:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"a{index}",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
        for index, text in enumerate(texts)
    ]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def run(monkeypatch, stdin: str, sent: list[tuple[str, str]]) -> int:  # type: ignore[no-untyped-def]
    """Drive `main` with `stdin`, recording what it tried to say.

    The fakes stand in for `send.enqueue`/`send.hush` and, unlike the real
    ones, record a blank text rather than dropping it -- so a hook that sends
    nothing is visible here as `("channel", "")` rather than as silence.
    """

    def fake_enqueue(source: str, text: str, **kw: object) -> str | None:
        sent.append((source, text))
        return None

    def fake_hush(source: str, **kw: object) -> str | None:
        sent.append((source, "<hush>"))
        return None

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr(hook, "enqueue", fake_enqueue)
    monkeypatch.setattr(hook, "hush", fake_hush)
    return hook.main([])


def test_stop_speaks_the_new_text(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Hello there.")
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert sent == [("claude-code:s1", "Hello there.")]
    assert capsys.readouterr().out == ""


def test_the_same_text_is_not_spoken_twice(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Once only.")
    sent: list[tuple[str, str]] = []
    run(monkeypatch, payload(transcript_path=str(path)), sent)
    run(monkeypatch, payload(transcript_path=str(path), hook_event_name="PostToolUse"), sent)
    assert sent == [("claude-code:s1", "Once only.")]


def test_post_tool_use_takes_the_same_path_as_stop(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Between tools.")
    sent: list[tuple[str, str]] = []
    run(monkeypatch, payload(transcript_path=str(path), hook_event_name="PostToolUse"), sent)
    assert sent == [("claude-code:s1", "Between tools.")]


def test_a_notification_speaks_on_the_notify_channel(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    body = payload(hook_event_name="Notification", message="Claude needs your permission.")
    assert run(monkeypatch, body, sent) == 0
    assert sent == [("claude-code:s1:notify", "Claude needs your permission.")]


def test_a_prompt_hushes_both_channels(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(hook_event_name="UserPromptSubmit"), sent) == 0
    assert sent == [("claude-code:s1", "<hush>"), ("claude-code:s1:notify", "<hush>")]


def test_nothing_new_writes_no_state(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path)
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert sent == []
    assert load("s1") is None


def test_garbage_on_stdin_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, "not json", sent) == 0
    assert sent == []
    assert capsys.readouterr().out == ""
    assert "not json" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def test_an_unknown_event_exits_zero_and_does_nothing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(hook_event_name="PreCompact"), sent) == 0
    assert sent == []


def test_an_exploding_send_still_exits_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Boom.")

    def explode(source: str, text: str, **kw: object) -> str | None:
        raise RuntimeError("the daemon caught fire")

    monkeypatch.setattr("sys.stdin", io.StringIO(payload(transcript_path=str(path))))
    monkeypatch.setattr(hook, "enqueue", explode)
    assert hook.main([]) == 0
    assert "caught fire" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def record(uuid: str, kind: str, text: str = "", sidechain: bool = False) -> str:
    body: dict[str, object] = {"type": kind, "uuid": uuid, "isSidechain": sidechain}
    if kind == "assistant":
        body["message"] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return json.dumps(body)


def append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


def test_repeated_hooks_drain_a_growing_transcript_exactly_once(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fire the hook over and over while the transcript grows underneath it.

    The layer below this one shipped a defect that no single call could
    show: parse returned nothing and consumed nothing, so the offset never
    moved and the session fell silent for good. The same shape is available
    here — a watermark that fails to advance, or one that rewinds — and only
    successive calls can rule it out. Three firings per turn stand in for the
    Stop that lands on top of a PostToolUse.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    sent: list[tuple[str, str]] = []
    offsets: list[int] = []

    for turn in range(6):
        append(path, record(f"u{turn}", "user"), record(f"a{turn}", "assistant", f"Turn {turn}."))
        for _ in range(3):
            assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
        mark = load("s1")
        assert mark is not None, f"turn {turn} left no watermark at all"
        offsets.append(mark.offset)

    assert [text for _, text in sent] == [f"Turn {turn}." for turn in range(6)]
    assert offsets == sorted(offsets), f"the watermark went backwards: {offsets}"
    # Every complete line consumed: nothing is left behind unread while the
    # hook reports there is nothing to say.
    assert offsets[-1] == path.stat().st_size

    # And it terminates: further firings against an unchanged file add nothing.
    before = len(sent)
    for _ in range(5):
        run(monkeypatch, payload(transcript_path=str(path)), sent)
    assert len(sent) == before


def test_a_half_written_line_is_left_for_the_next_hook(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Claude Code appends while we read, so a trailing partial line is normal.

    It must not be consumed (its text would be lost) and it must not wedge
    the offset (everything after it would be). Both are only visible across
    the two calls.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    sent: list[tuple[str, str]] = []

    append(path, record("u0", "user"), record("a0", "assistant", "Complete."))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record("a1", "assistant", "Half writ"))  # no newline yet
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert [text for _, text in sent] == ["Complete."]
    mark = load("s1")
    assert mark is not None and mark.offset < path.stat().st_size

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert [text for _, text in sent] == ["Complete.", "Half writ"]
    mark = load("s1")
    assert mark is not None and mark.offset == path.stat().st_size


def test_a_corrupt_line_does_not_stop_the_drain(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One unreadable line must cost one line, not the rest of the session."""
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    sent: list[tuple[str, str]] = []

    append(path, record("u0", "user"), record("a0", "assistant", "Before."))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assist\rtant", not json at all\n')
    append(path, record("a2", "assistant", "After."))

    for _ in range(3):
        assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert [text for _, text in sent] == ["Before.\n\nAfter."]
    mark = load("s1")
    assert mark is not None and mark.offset == path.stat().st_size


def test_sub_agent_records_are_silent_but_still_advance_the_watermark(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The sidechain case: nothing to say, yet state must still be written.

    `new_text` hands back an empty string with a real watermark here. A
    caller that took "no text" for "nothing happened" and skipped the save
    would rescan every sub-agent record on every hook for the rest of the
    session — growing work, and the drain would never terminate.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    sent: list[tuple[str, str]] = []

    append(path, record("u0", "user"))
    for index in range(4):
        append(path, record(f"s{index}", "assistant", f"Sub-agent {index}.", sidechain=True))

    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    # The blank string `new_text` returned does reach `enqueue`, which drops
    # it without opening a connection -- that guard lives in `send` and is
    # tested there. What must never happen is a sub-agent's words going out.
    assert [text for _, text in sent if text.strip()] == []
    assert "Sub-agent" not in "".join(text for _, text in sent)
    mark = load("s1")
    assert mark is not None, "a sidechain-only turn wrote no watermark"
    assert mark.offset == path.stat().st_size

    append(path, record("a1", "assistant", "The main agent speaks."))
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    # Only the new line. The sidechain block was consumed, not re-walked.
    assert [(source, text) for source, text in sent if text.strip()] == [
        ("claude-code:s1", "The main agent speaks.")
    ]


def test_two_concurrent_hooks_do_not_both_speak_the_same_text(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Claude Code fires PostToolUse concurrently for parallel tool calls.

    Nothing below this module enforces the lock, so this is where it is
    checked: the second hook must wait out the first's save rather than read
    the watermark it is about to replace.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Exactly once.")
    sent: list[tuple[str, str]] = []
    guard = threading.Lock()
    holding = threading.Event()

    def slow_enqueue(source: str, text: str, **kw: object) -> str | None:
        first = not holding.is_set()
        holding.set()
        if first:
            # Still inside the lock, and long enough that an unlocked second
            # hook would certainly read the watermark before this one saves.
            time.sleep(0.4)
        with guard:
            sent.append((source, text))
        return None

    monkeypatch.setattr(hook, "enqueue", slow_enqueue)
    body = json.loads(payload(transcript_path=str(path)))

    first = threading.Thread(target=hook._dispatch, args=(body,))
    first.start()
    assert holding.wait(timeout=5.0), "the first hook never reached its enqueue"
    second = threading.Thread(target=hook._dispatch, args=(body,))
    second.start()
    first.join(timeout=10.0)
    second.join(timeout=10.0)
    assert not first.is_alive() and not second.is_alive()
    assert sent == [("claude-code:s1", "Exactly once.")]


def test_no_event_ever_writes_to_stdout(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """UserPromptSubmit stdout is injected into the model's context.

    The others surface in the transcript. So every branch, including the
    ones that fail, is checked rather than only the two the happy path
    covers.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Something to say.")
    sent: list[tuple[str, str]] = []
    bodies = [
        payload(transcript_path=str(path)),
        payload(transcript_path=str(path), hook_event_name="PostToolUse"),
        payload(hook_event_name="Notification", message="Permission needed."),
        payload(hook_event_name="UserPromptSubmit"),
        payload(hook_event_name="PreCompact"),
        payload(transcript_path=""),
        "not json",
        "[1, 2, 3]",
        "",
    ]
    for body in bodies:
        assert run(monkeypatch, body, sent) == 0
        captured = capsys.readouterr()
        assert captured.out == "", f"{body[:60]!r} printed {captured.out!r}"


def test_a_payload_that_blows_the_parser_still_exits_zero(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """`json.loads` does not only raise JSONDecodeError.

    Deeply nested JSON exhausts the C stack and comes out as RecursionError,
    which is neither of the two exceptions the parse is guarded against. The
    top level has to be the backstop, not the parse.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, "[" * 200_000, sent) == 0
    assert sent == []
    assert "RecursionError" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def test_no_payload_ever_reaches_claude_code_as_a_traceback(tmp_path: Path) -> None:
    """The console script, in its own process, which is how it actually runs.

    A traceback on stderr surfaces in the transcript and a non-zero exit is a
    hook failure, so this is asserted end to end rather than through `main`'s
    return value alone.
    """
    venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin"
    entry = venv_bin / "speakd-claude-hook"
    assert entry.exists(), "run `uv sync --group dev` first"
    hostile = {
        "deeply nested": "[" * 200_000,
        "not json": "not json at all",
        "empty": "",
        "a bare list": "[1, 2, 3]",
        "a lone number": "17",
        "null": "null",
        "a truncated object": '{"session_id": ',
        "invalid utf-8ish": "\udcff".encode("utf-8", "surrogateescape").decode("latin-1"),
    }
    for name, body in hostile.items():
        done = sp.run(
            [str(entry)],
            input=body,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path / "home"),
                "SPEAKD_STATE_DIR": str(tmp_path / "state"),
            },
        )
        assert done.returncode == 0, f"{name}: exit {done.returncode}"
        assert done.stdout == "", f"{name}: printed {done.stdout!r}"
        assert done.stderr == "", f"{name}: stderr {done.stderr[-300:]!r}"


def test_a_base_exception_is_survived_but_an_interrupt_is_re_raised(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Narrowing the backstop to `Exception` passes every other test here.

    RecursionError happens to be an Exception, so the test above cannot tell
    the two spellings apart. MemoryError is not, and neither is anything a
    future caller raises off the Exception tree — those are the ones worth
    surviving. KeyboardInterrupt and SystemExit are the two that are not
    ours to swallow: something asked this process to stop.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))

    class Sudden(BaseException):
        """Off the Exception tree entirely, as MemoryError is."""

    def raise_sudden() -> None:
        raise Sudden("the floor gave way")

    monkeypatch.setattr(hook, "_read_and_dispatch", raise_sudden)
    assert hook.main([]) == 0
    assert "the floor gave way" in (state_dir() / "hook.log").read_text(encoding="utf-8")

    for interrupt in (KeyboardInterrupt, SystemExit):

        def raise_interrupt(which: type[BaseException] = interrupt) -> None:
            raise which()

        monkeypatch.setattr(hook, "_read_and_dispatch", raise_interrupt)
        with pytest.raises(interrupt):
            hook.main([])


def test_the_prompt_path_hushes_on_the_shorter_timeout(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Both hushes must carry the shorter budget, not just exist."""
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    seen: list[object] = []

    def recording_hush(source: str, **kw: object) -> str | None:
        seen.append(kw.get("timeout"))
        return None

    monkeypatch.setattr("sys.stdin", io.StringIO(payload(hook_event_name="UserPromptSubmit")))
    monkeypatch.setattr(hook, "hush", recording_hush)
    assert hook.main([]) == 0
    assert seen == [hook.HUSH_TIMEOUT, hook.HUSH_TIMEOUT]
    assert hook.HUSH_TIMEOUT < send_module.TIMEOUT, "the prompt path must be the quicker one"


def test_a_prompt_against_an_unreachable_daemon_stays_inside_its_budget(tmp_path: Path) -> None:
    """The real measurement, in a real process, against a real dead socket.

    A socket that listens and never accepts is the worst case: both hushes
    pay their full timeout. Run through the console script so interpreter
    start-up is counted, because Claude Code counts it.
    """
    runtime = tmp_path / "run" / "speakd"
    runtime.mkdir(parents=True)
    listener = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    listener.bind(str(runtime / "speakd.sock"))
    listener.listen(2)
    entry = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "speakd-claude-hook"
    body = payload(hook_event_name="UserPromptSubmit")
    try:
        started = time.monotonic()
        done = sp.run(
            [str(entry)],
            input=body,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path / "home"),
                "SPEAKD_STATE_DIR": str(tmp_path / "state"),
                "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            },
        )
        elapsed = time.monotonic() - started
    finally:
        listener.close()
    assert done.returncode == 0
    assert done.stdout == ""
    assert elapsed < PROMPT_BUDGET, (
        f"took {elapsed:.2f}s against the manifest's {PROMPT_BUDGET}s for UserPromptSubmit"
    )
