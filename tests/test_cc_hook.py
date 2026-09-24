"""Tests for the hook entry point's dispatch and failure policy."""

import io
import json
import socket as socketlib
import subprocess as sp
import time
from pathlib import Path

import pytest

from speakd.clients import send as send_module
from speakd.clients.claude_code import hook, registry
from speakd.clients.claude_code.watermark import state_dir
from speakd.clients.log import LOG_CAP_BYTES

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
        "hook_event_name": "UserPromptSubmit",
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
        sent.append((source, text, kw.get("kind"), kw.get("flags")))
        return None

    def fake_hush(source: str, **kw: object) -> str | None:
        sent.append((source, "<hush>"))
        return None

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr(hook, "enqueue", fake_enqueue)
    monkeypatch.setattr(hook, "hush", fake_hush)
    return hook.main([])


def test_a_notification_is_an_attention_on_the_main_channel(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list = []  # type: ignore[type-arg]
    body = payload(hook_event_name="Notification", message="Claude needs your permission.")
    assert run(monkeypatch, body, sent) == 0
    assert sent == [("claude-code:s1", "Claude needs your permission.", "attention", None)]


def test_a_prompt_hushes_the_session(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(hook_event_name="UserPromptSubmit"), sent) == 0
    assert sent == [("claude-code:s1", "<hush>")]


def test_post_tool_use_no_longer_speaks(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The follower sees the message land before the tool after it finishes.

    So a hook here would only say the same thing later -- at the cost of a
    Python start-up inside the user's turn, once per tool call. An install
    still carrying the old manifest fires this; it has to be a no-op, not a
    second voice.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Hello there.")
    sent: list[tuple[str, str]] = []
    body = payload(transcript_path=str(path), hook_event_name="PostToolUse")
    assert run(monkeypatch, body, sent) == 0
    assert sent == []


def test_stop_says_finished_unless_the_agent_briefed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The backstop for a turn that ended without a briefing.

    Only in brief mode -- in full mode the response was just read aloud --
    and only when the agent said nothing itself; the daemon judges both.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list = []  # type: ignore[type-arg]
    assert run(monkeypatch, payload(hook_event_name="Stop"), sent) == 0
    assert sent == [
        (
            "claude-code:s1",
            "finished",
            "attention",
            {"unless_briefed": True, "only_in_mode": "brief"},
        )
    ]


def test_a_prompt_records_the_claude_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "_claude_pid", lambda: 4242)
    run(monkeypatch, payload(hook_event_name="UserPromptSubmit"), [])
    assert [r.claude_pid for r in registry.live()] == [4242]


def test_a_prompt_registers_the_session(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """This is the only place the follower learns a session exists.

    Nothing else in the system knows a transcript's path, so a prompt that
    hushed without registering would leave a live session unspoken for as
    long as it ran.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path="/tmp/t.jsonl", cwd="/home/me/p"), sent) == 0
    found = registry.live()
    assert [r.session_id for r in found] == ["s1"]
    assert found[0].transcript == Path("/tmp/t.jsonl")
    assert found[0].cwd == "/home/me/p"


def test_the_session_is_registered_before_the_hushes_go_out(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Order, because the hushes are the part that can go slowly.

    Each one pays its full timeout against a daemon that is listening but
    wedged, and Claude Code kills the hook at the manifest's budget. A
    registration written after them is one that never gets written on the
    turn that needed it most -- the first, where the session is still
    unknown. The local file write cannot block on a socket, so it goes first.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    seen: list[list[str]] = []

    def recording_hush(source: str, **kw: object) -> str | None:
        seen.append([r.session_id for r in registry.live()])
        return None

    monkeypatch.setattr(hook, "hush", recording_hush)
    hook._dispatch(
        {
            "session_id": "s1",
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": "/p",
        }
    )
    assert seen == [["s1"]]


def test_a_prompt_without_a_transcript_path_still_hushes(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Registering is the new work; stopping the speech is the old promise.

    A payload with no transcript path registers nothing -- there is nothing
    to follow -- but the user pressing enter still means silence.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path=""), sent) == 0
    assert sent == [("claude-code:s1", "<hush>")]
    assert registry.live() == []


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

    def explode(source: str, text: str, **kw: object) -> str | None:
        raise RuntimeError("the daemon caught fire")

    body = payload(hook_event_name="Notification", message="Permission needed.")
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    monkeypatch.setattr(hook, "enqueue", explode)
    assert hook.main([]) == 0
    assert "caught fire" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def test_no_event_ever_writes_to_stdout(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """UserPromptSubmit stdout is injected into the model's context.

    The others surface in the transcript. So every branch is checked, the
    ones that fail and the two events we no longer act on -- an install
    carrying the old manifest still fires those, and a print on that path
    would reach the model as if the user had typed it.
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
    assert seen == [hook.HUSH_TIMEOUT]
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


def _run_failing(  # type: ignore[no-untyped-def]
    monkeypatch,
    stdin: str,
    sent: list[tuple[str, str]],
    reason: str = "no daemon at /run/user/1000/speakd/speakd.sock",
) -> int:
    """Like `run`, but the daemon refuses everything, as a stopped one does."""

    def failing_enqueue(source: str, text: str, **kw: object) -> str | None:
        sent.append((source, text, kw.get("kind"), kw.get("flags")))
        return reason

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr(hook, "enqueue", failing_enqueue)
    return hook.main([])


def test_a_failed_notification_is_logged(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The daemon-not-running case, which is the ordinary one.

    The reason is the only diagnostic a user gets, and the README sends them
    to this file; dropping it turns "speakd is not running" into silence with
    no explanation at all.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    body = payload(hook_event_name="Notification", message="Permission needed.")
    assert _run_failing(monkeypatch, body, sent) == 0
    assert sent == [("claude-code:s1", "Permission needed.", "attention", None)]
    assert "no daemon" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def test_the_log_is_capped_rather_than_growing_without_end(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """With the daemon off, something writes a line to this file forever.

    It used to be every PostToolUse hook, measured at 1.8 MB per twenty
    thousand of them and nothing ever trimmed it. It is now the follower,
    which polls ten times a second and so gets there faster. A cap, not
    rotation: one file, and the newest failures are the ones someone tailing
    it actually needs.
    """
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    log = state_dir() / "hook.log"
    for index in range(4000):
        hook._log(f"line {index} " + "x" * 120)
    size = log.stat().st_size
    assert size <= LOG_CAP_BYTES * 2, f"the log reached {size} bytes"

    written = log.read_text(encoding="utf-8")
    assert "line 3999" in written, "the cap threw away the newest entry, not the oldest"
    assert "earlier entries dropped" in written, "a truncated log must say it was truncated"


def test_the_cap_leaves_a_short_log_alone(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The ordinary case is a handful of lines, and they must all survive."""
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    for index in range(20):
        hook._log(f"entry {index}")
    written = (state_dir() / "hook.log").read_text(encoding="utf-8")
    assert all(f"entry {index}" in written for index in range(20))
    assert "earlier entries dropped" not in written
