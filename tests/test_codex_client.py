"""Codex's rollout format and lifecycle hooks, without a running agent."""

import io
import json
from pathlib import Path
from types import ModuleType

import pytest

from speakd.clients import registry
from speakd.protocol import Request, Response

Sent = list[tuple[str, str, dict[str, object]]]


def record(text: str, *, phase: str = "commentary", role: str = "assistant") -> bytes:
    return (
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "phase": phase,
                    "content": [{"type": "output_text", "text": text}],
                },
            }
        )
        + "\n"
    ).encode()


def test_rollout_speaks_only_assistant_prose_once() -> None:
    from speakd.clients.codex.transcript import parse

    chunk = (
        record("Progress.")
        + record("Answer.", phase="final")
        + record("Private reasoning", phase="analysis")
        + record("Prompt", role="user")
        + b'{"type":"event_msg","payload":{"type":"agent_message","message":"Answer."}}\n'
        + b'{"type":"response_item","payload":{"type":"reasoning","text":"Secret"}}\n'
        + b"not json\n"
    )
    text, consumed = parse(chunk + record("Partial.")[:-3])
    assert text == "Progress.\n\nAnswer."
    assert consumed == len(chunk)


def test_unknown_and_corrupt_complete_lines_advance_the_cursor() -> None:
    from speakd.clients.codex.transcript import parse

    chunk = b"{}\n\xff\n[]\n"
    assert parse(chunk) == ("", len(chunk))


def test_new_codex_channel_starts_muted_and_can_brief() -> None:
    from speakd.__main__ import build_channels
    from speakd.channels import effective_mode

    channel = build_channels().open("codex:thread-1")
    assert (channel.muted, channel.briefs, effective_mode(channel)) == (True, True, "brief")


def setup_hook(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Sent]:
    from speakd.clients.codex import hook

    sent: Sent = []
    monkeypatch.setattr(
        hook, "_send", lambda verb, source, payload: sent.append((verb, source, payload))
    )
    return hook, sent


def test_prompt_registers_hushes_and_supplies_explicit_mcp_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook, sent = setup_hook(monkeypatch)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(record("Old answer."))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread-1",
                    "transcript_path": str(transcript),
                    "cwd": "/work/repo",
                }
            )
        ),
    )
    assert hook.main() == 0
    reg = next(r for r in registry.live() if r.client == "codex")
    assert (reg.session_id, reg.transcript, reg.cwd) == ("thread-1", transcript, "/work/repo")
    assert sent == [("hush", "codex:thread-1", {"new_turn": True})]
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    assert "codex:thread-1" in context["additionalContext"]
    assert "source_id" in context["additionalContext"]


@pytest.mark.parametrize(
    "event,verb,flags",
    [
        ("Interrupt", "hush", {}),
        ("PermissionRequest", "enqueue", {"kind": "attention"}),
        ("Stop", "enqueue", {"kind": "attention", "unless_briefed": True, "only_in_mode": "brief"}),
    ],
)
def test_control_events(
    event: str, verb: str, flags: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    hook, sent = setup_hook(monkeypatch)
    hook.dispatch({"hook_event_name": event, "session_id": "thread-1"})
    assert len(sent) == 1
    actual_verb, source, payload = sent[0]
    assert (actual_verb, source) == (verb, "codex:thread-1")
    assert {key: payload[key] for key in flags} == flags


@pytest.mark.parametrize("body", ["not json", "[]", '{"hook_event_name":"Stop"}'])
def test_bad_hook_input_never_fails_or_speaks(
    body: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook, sent = setup_hook(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert hook.main() == 0
    assert sent == []
    assert capsys.readouterr().out == ""


def test_follower_catches_fast_response_without_replaying_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.codex.follow import Follower

    hook, _ = setup_hook(monkeypatch)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(record("History."))
    hook.dispatch(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "transcript_path": str(transcript),
            "cwd": "/repo",
        }
    )
    with transcript.open("ab") as handle:
        handle.write(record("Fast reply."))
    sent: Sent = []
    follower = Follower(send=lambda *args: sent.append(args))
    follower.tick()
    follower.tick()
    assert [p["text"] for v, _, p in sent if v == "enqueue"] == ["Fast reply."]
    assert {s for _, s, _ in sent} == {"codex:thread-1"}


def test_follower_retries_failed_sends_and_waits_for_complete_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.codex.follow import Follower

    hook, _ = setup_hook(monkeypatch)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(b"")
    hook.dispatch(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "transcript_path": str(transcript),
        }
    )
    whole = record("Whole sentence.")
    transcript.write_bytes(whole[:-4])
    sent: list[str] = []
    failures = [True, False]

    def send(verb: str, source: str, payload: dict[str, object]) -> str | None:
        if verb == "enqueue":
            if failures.pop(0):
                return "offline"
            sent.append(str(payload["text"]))
        return None

    follower = Follower(send=send)
    follower.tick()
    assert sent == []
    with transcript.open("ab") as handle:
        handle.write(whole[-4:])
    follower.tick()
    assert sent == []
    follower.tick()
    follower.tick()
    assert sent == ["Whole sentence."]


def test_interrupt_discards_late_output_until_a_new_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.codex.follow import Follower

    hook, _ = setup_hook(monkeypatch)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(b"")
    body = {"session_id": "thread-1", "transcript_path": str(transcript)}
    hook.dispatch({**body, "hook_event_name": "UserPromptSubmit"})
    hook.dispatch({**body, "hook_event_name": "Interrupt"})
    transcript.write_bytes(record("Cancelled output."))
    sent: Sent = []
    follower = Follower(send=lambda *args: sent.append(args))
    follower.tick()
    assert not [p for v, _, p in sent if v == "enqueue"]
    hook.dispatch({**body, "hook_event_name": "UserPromptSubmit"})
    with transcript.open("ab") as handle:
        handle.write(record("New turn."))
    follower.tick()
    assert [p["text"] for v, _, p in sent if v == "enqueue"] == ["New turn."]


def test_codex_mcp_call_uses_explicit_source_instead_of_shared_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.mcp import server, session

    requests: list[Request] = []

    def call(request: Request, **kwargs: object) -> Response:
        requests.append(request)
        return Response(ok=True, data={"spoken": True})

    monkeypatch.setattr(server, "call", call)
    monkeypatch.setattr(session, "resolve", lambda *a, **kw: ("codex:wrong-thread", None))
    mcp = server.Server(None)
    for source in ("codex:first", "codex:second"):
        mcp.handle(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "brief",
                    "arguments": {"source_id": source, "text": "Done.", "kind": "done"},
                },
            }
        )
    assert [r.source_id for r in requests if r.verb.value == "enqueue"] == [
        "codex:first",
        "codex:second",
    ]


def test_follower_recovers_prompt_offset_when_cursor_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.codex import state
    from speakd.clients.codex.follow import Follower

    hook, _ = setup_hook(monkeypatch)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(record("Old history."))
    hook.dispatch(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "transcript_path": str(transcript),
        }
    )
    with transcript.open("ab") as handle:
        handle.write(record("Fast answer."))
    next(state.state_dir().glob("*.cursor.json")).write_text("bad json")
    sent: Sent = []
    Follower(send=lambda *args: sent.append(args)).tick()
    assert [p["text"] for v, _, p in sent if v == "enqueue"] == ["Fast answer."]


def test_prompt_without_transcript_disables_previous_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.codex.follow import Follower

    hook, _ = setup_hook(monkeypatch)
    transcript = tmp_path / "old.jsonl"
    transcript.write_bytes(b"")
    hook.dispatch(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "transcript_path": str(transcript),
        }
    )
    hook.dispatch(
        {"hook_event_name": "UserPromptSubmit", "session_id": "thread-1", "transcript_path": None}
    )
    transcript.write_bytes(record("Stale output."))
    sent: Sent = []
    Follower(send=lambda *args: sent.append(args)).tick()
    assert [p for v, _, p in sent if v == "enqueue"] == []


def test_codex_mcp_call_without_identity_never_guesses_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speakd.clients.mcp import server, session

    sent: list[Request] = []

    def call(request: Request, **kwargs: object) -> Response:
        sent.append(request)
        return Response(ok=True)

    monkeypatch.setattr(server, "call", call)
    monkeypatch.setattr(session, "resolve", lambda *a, **kw: ("codex:wrong-thread", None))
    answer = server.Server(None).handle(
        {
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "brief",
                "arguments": {"text": "Private update.", "kind": "done"},
            },
        }
    )
    assert answer is not None
    result = answer["result"]
    assert isinstance(result, dict) and result["isError"] is True
    assert sent == []
