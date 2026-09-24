"""Tests for starting the HTTP transport alongside the daemon."""

import socket

import pytest

from speakd import __main__ as main_module
from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine


def daemon() -> Daemon:
    return Daemon(
        FakeEngine(),
        RecordingPlayer(),
        lambda n: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )


@pytest.mark.parametrize(
    ("value", "port"), [(None, 8642), ("9000", 9000), ("0", None), ("off", None), ("-3", None)]
)
def test_the_port_comes_from_the_environment(monkeypatch, value, port) -> None:  # type: ignore[no-untyped-def]
    if value is None:
        monkeypatch.delenv("SPEAKD_HTTP_PORT", raising=False)
    else:
        monkeypatch.setenv("SPEAKD_HTTP_PORT", value)
    assert main_module.http_port() == port


def test_an_out_of_range_port_is_reported(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """`-3` and `86420` are both syntactically fine integers but not ports;
    `int()` accepts them, so unlike garbage input they must be caught
    separately -- and, like garbage input, said out loud rather than just
    disabling HTTP silently."""
    monkeypatch.setenv("SPEAKD_HTTP_PORT", "86420")
    assert main_module.http_port() is None
    assert "86420" in capsys.readouterr().err

    monkeypatch.setenv("SPEAKD_HTTP_PORT", "-3")
    assert main_module.http_port() is None
    assert "-3" in capsys.readouterr().err


def test_the_transport_starts_and_mints_a_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd import http_token

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    monkeypatch.setenv("SPEAKD_HTTP_PORT", str(free))
    server = main_module.start_http(daemon())
    try:
        assert server is not None
        assert server.port == free
        assert http_token.read() is not None
    finally:
        if server is not None:
            server.stop()


def test_a_taken_port_is_reported_and_the_daemon_runs_on(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        monkeypatch.setenv("SPEAKD_HTTP_PORT", str(taken.getsockname()[1]))
        assert main_module.start_http(daemon()) is None
    assert "HTTP" in capsys.readouterr().err
