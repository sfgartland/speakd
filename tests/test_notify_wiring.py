"""That the connector is actually started, and that the starter file matches the defaults.

Both are drift guards. `STARTER_TOML` is written by hand for the sake of its
comments, so nothing but a test keeps it saying what `DEFAULT_RULES` says; and
a connector nobody spawns is a feature that exists only in its own tests.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import pytest
import tomllib

from speakd import __main__ as entrypoint
from speakd.clients.notifications import rules
from speakd.clients.notifications.monitor import Notification
from speakd.clients.notifications.starter import STARTER_TOML


def _starter_ruleset(tmp_path: Path) -> rules.Ruleset:
    path = tmp_path / "notifications.toml"
    path.write_text(STARTER_TOML, encoding="utf-8")
    return rules.load_rules(path)


def test_the_starter_file_is_valid_toml() -> None:
    tomllib.load(io.BytesIO(STARTER_TOML.encode("utf-8")))


def test_the_starter_file_loads_as_rules(tmp_path: Path) -> None:
    assert _starter_ruleset(tmp_path).rules


@pytest.mark.parametrize(
    ("app", "summary", "body"),
    [
        ("Google Chrome", "Mamma", "are we still on for tomorrow?"),
        ("Chromium", "Mamma", "are we still on for tomorrow?"),
        ("Thunderbird", "New message", "KU Leuven -- exam schedule"),
        ("Evolution", "New message", "KU Leuven -- exam schedule"),
        ("Steam", "Sale", "half price"),
    ],
)
def test_the_starter_file_decides_the_same_way_as_the_built_in_defaults(
    tmp_path: Path, app: str, summary: str, body: str
) -> None:
    note = Notification(app=app, summary=summary, body=body)
    starter = rules.decide(note, _starter_ruleset(tmp_path))
    built_in = rules.decide(note, rules.DEFAULT_RULES)
    assert starter.speak == built_in.speak, f"{app} is treated differently by the two"
    assert starter.channel == built_in.channel


def test_the_defaults_speak_what_they_were_asked_to_speak() -> None:
    """The regression test for "the feature does nothing on the day it is installed"."""
    chrome = rules.decide(
        Notification(app="Google Chrome", summary="Mamma", body="tomorrow?"),
        rules.DEFAULT_RULES,
    )
    mail = rules.decide(
        Notification(app="Thunderbird", summary="New message", body="exam schedule"),
        rules.DEFAULT_RULES,
    )
    assert chrome.speak and mail.speak
    assert "Mamma" in chrome.text
    assert "exam schedule" in mail.text


def test_an_app_nobody_asked_for_is_silent_by_default() -> None:
    """An allow list, so a newly chatty app cannot start talking on its own."""
    verdict = rules.decide(
        Notification(app="Steam", summary="Sale", body="half price"), rules.DEFAULT_RULES
    )
    assert verdict.speak is False


class _FakeSupervisor:
    started: list[Sequence[str]] = []

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = argv

    def start(self) -> None:
        _FakeSupervisor.started.append(self.argv)


@pytest.fixture
def _fake_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSupervisor.started = []
    monkeypatch.setattr(entrypoint, "Supervisor", _FakeSupervisor)
    # conftest sets both of these for every test; this file is the one place
    # that needs them cleared to see what the daemon would really start.
    monkeypatch.delenv("SPEAKD_NO_FOLLOWER", raising=False)
    monkeypatch.delenv("SPEAKD_NO_NOTIFY", raising=False)


def _modules() -> list[str]:
    return [argv[-1] for argv in _FakeSupervisor.started]


@pytest.mark.usefixtures("_fake_supervisor")
def test_the_daemon_starts_both_connectors() -> None:
    entrypoint._start_children()
    assert _modules() == [
        "speakd.clients.claude_code.follow",
        "speakd.clients.notifications.follow",
    ]


@pytest.mark.usefixtures("_fake_supervisor")
def test_each_connector_can_be_suppressed_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEAKD_NO_NOTIFY", "1")
    entrypoint._start_children()
    assert _modules() == ["speakd.clients.claude_code.follow"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_suppressing_notifications_does_not_suppress_claude_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEAKD_NO_FOLLOWER", "1")
    entrypoint._start_children()
    assert _modules() == ["speakd.clients.notifications.follow"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_the_test_suite_itself_spawns_no_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector escaping a test run reads the developer's own mail aloud."""
    monkeypatch.setenv("SPEAKD_NO_FOLLOWER", "1")
    monkeypatch.setenv("SPEAKD_NO_NOTIFY", "1")
    assert entrypoint._start_children() == []
