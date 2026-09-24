"""Tests for the MCP server agents brief through."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from speakd.channels import ChannelTable
from speakd.clients.mcp import guide, session
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


@pytest.fixture
def daemon_socket() -> Iterator[tuple[Path, Daemon]]:
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        lambda n: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    d.start()
    # Short and in /tmp: AF_UNIX paths are limited to about a hundred bytes.
    path = Path("/tmp") / f"speakd-mcp-test-{os.getpid()}-{id(d)}.sock"
    server = SocketServer(path, d.handle, d.bus)
    server.start()
    try:
        yield path, d
    finally:
        server.stop()
        d.stop()


Proc = subprocess.Popen  # type: ignore[type-arg]


def rpc(proc: Proc, method: str, params: dict[str, object] | None = None, id_: int = 1) -> dict:  # type: ignore[type-arg]
    message = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())  # type: ignore[no-any-return]


def call(proc: Proc, name: str, arguments: dict[str, object], id_: int) -> dict:  # type: ignore[type-arg]
    return rpc(proc, "tools/call", {"name": name, "arguments": arguments}, id_)["result"]  # type: ignore[no-any-return]


def spawn(socket: Path, tmp_path: Path) -> Proc:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "SPEAKD_STATE_DIR": str(tmp_path / "st"),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "speakd.clients.mcp.server", "--socket", str(socket)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_initialize_hands_over_the_guide(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, _ = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        init = {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex"}}
        result = rpc(proc, "initialize", init)["result"]
        assert result["serverInfo"]["name"] == "speakd"
        assert result["protocolVersion"] == "2025-06-18"
        assert "brief" in result["instructions"]
        tools = rpc(proc, "tools/list", id_=2)["result"]["tools"]
        assert {t["name"] for t in tools} == {"brief", "briefing_status", "set_mode"}
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_brief_answers_with_the_mode(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, _ = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        out = call(proc, "brief", {"text": "Tests pass.", "kind": "done"}, 2)
        assert out["isError"] is False
        assert out["structuredContent"]["mode"] == "brief"
        assert out["structuredContent"]["spoken"] is True
        status = call(proc, "briefing_status", {}, 3)["structuredContent"]
        assert status["briefs"] is True
        assert "brief" in status["guide"]
        switched = call(proc, "set_mode", {"mode": "full"}, 4)
        assert switched["structuredContent"]["mode"] == "full"
        again = call(proc, "brief", {"text": "More.", "kind": "done"}, 5)
        assert again["structuredContent"]["spoken"] is False
        assert again["structuredContent"]["mode"] == "full"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_blank_unknown_and_malformed_do_not_crash(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, _ = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        blank = call(proc, "brief", {"text": "  ", "kind": "done"}, 2)
        assert blank["structuredContent"]["spoken"] is False
        assert call(proc, "nope", {}, 3)["isError"] is True
        assert call(proc, "brief", {}, 4)["isError"] is True
        proc.stdin.write("{not json\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["error"]["code"] == -32700
        assert rpc(proc, "no/such/method", {}, 5)["error"]["code"] == -32601
        assert "result" in rpc(proc, "ping", {}, 6)
    finally:
        proc.stdin.close()
    assert proc.wait(timeout=5) == 0  # EOF ends it cleanly


def test_no_daemon_is_an_answer_not_an_error(tmp_path: Path) -> None:
    proc = spawn(tmp_path / "absent.sock", tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        out = call(proc, "brief", {"text": "Hi.", "kind": "done"}, 2)
        assert out["isError"] is False
        assert out["structuredContent"]["spoken"] is False
        assert "not running" in out["structuredContent"]["reason"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def fake_proc(root: Path, chain: list[tuple[int, str, int]]) -> Path:
    """Write /proc/<pid>/stat and comm for a chain of (pid, comm, ppid)."""
    for pid, comm, ppid in chain:
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
        (root / str(pid) / "comm").write_text(comm + "\n")
    (root / "self").symlink_to(root / str(chain[0][0]))
    return root


def test_a_claude_code_session_is_found_by_ancestry(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.clients.claude_code import registry

    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    proc = fake_proc(
        tmp_path / "proc", [(300, "python3", 200), (200, "claude", 100), (100, "zsh", 1)]
    )
    registry.register("abc", Path("/t.jsonl"), "/work", claude_pid=200)
    assert session.resolve("claude-code", proc=proc) == ("claude-code:abc", None)


def test_anything_else_gets_a_channel_of_its_own(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    proc = fake_proc(tmp_path / "proc", [(300, "python3", 250), (250, "codex", 1)])
    assert session.resolve("codex", proc=proc, cwd="/home/u/kronikk") == (
        "agent:codex:250",
        "codex · kronikk",
    )


def test_the_guide_is_the_users_file_when_there_is_one(tmp_path: Path) -> None:
    assert "brief" in guide.text(tmp_path)
    (tmp_path / "speakd").mkdir()
    (tmp_path / "speakd" / "briefing.md").write_text("Only tell me when it is done.")
    assert guide.text(tmp_path) == "Only tell me when it is done."
