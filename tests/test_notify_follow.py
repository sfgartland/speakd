"""The loop between the bus and the daemon: what it sends, and what it refuses to."""

from __future__ import annotations

from pathlib import Path

import pytest

from speakd.clients.notifications import history, rules
from speakd.clients.notifications.follow import Narrator, load_ruleset
from speakd.clients.notifications.monitor import Notification


class _Recorder:
    """A `send` that remembers, and can be told to fail."""

    def __init__(self, failure: str | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.failure = failure

    def __call__(self, verb: str, source: str, payload: dict[str, object]) -> str | None:
        self.calls.append((verb, source, payload))
        return self.failure if verb == "enqueue" else None

    def enqueues(self) -> list[tuple[str, dict[str, object]]]:
        return [(source, payload) for verb, source, payload in self.calls if verb == "enqueue"]


def _ruleset(*rule: rules.Rule, per_minute: int = 20) -> rules.Ruleset:
    return rules.Ruleset(rules=rule, max_per_minute=per_minute)


def _chrome() -> rules.Rule:
    return rules.Rule(name="chrome", app="*chrome*", say="{summary} says. {body}")


def _note(
    app: str = "Google Chrome", summary: str = "Mamma", body: str = "tomorrow?"
) -> Notification:
    return Notification(app=app, summary=summary, body=body, when=100.0)


def test_a_matching_notification_is_enqueued_with_its_rendered_text() -> None:
    send = _Recorder()
    Narrator(_ruleset(_chrome()), send=send).handle(_note())
    [(source, payload)] = send.enqueues()
    assert source == "notify:google-chrome"
    assert payload["text"] == "Mamma says. tomorrow?"
    assert payload["profile"] == "notification"
    # `kind` so a channel put into the background can be told to interrupt
    # for notifications and nothing else.
    assert payload["kind"] == "notification"


def test_the_channel_is_named_and_ranked_once_not_once_per_notification() -> None:
    send = _Recorder()
    narrator = Narrator(_ruleset(_chrome()), send=send)
    for _ in range(3):
        narrator.handle(_note())
    labels = [c for c in send.calls if c[0] == "set_label"]
    priorities = [c for c in send.calls if c[0] == "set_priority"]
    assert len(labels) == 1, "a label re-sent per notification is a round trip per message"
    assert len(priorities) == 1
    assert labels[0][2]["label"] == "Google Chrome"
    assert len(send.enqueues()) == 3


def test_each_app_gets_its_own_channel() -> None:
    """So that muting WhatsApp does not also mute your mail."""
    send = _Recorder()
    narrator = Narrator(
        _ruleset(_chrome(), rules.Rule(name="mail", app="thunderbird")),
        send=send,
    )
    narrator.handle(_note())
    narrator.handle(_note(app="Thunderbird"))
    assert {source for source, _ in send.enqueues()} == {
        "notify:google-chrome",
        "notify:thunderbird",
    }


def test_a_rule_that_declines_speaks_nothing_but_is_still_recorded() -> None:
    send = _Recorder()
    quiet = rules.Rule(name="hush-chrome", app="*chrome*", speak=False)
    Narrator(_ruleset(quiet, _chrome()), send=send).handle(_note())
    assert send.enqueues() == []
    [entry] = history.recent()
    assert entry.spoken is False
    assert entry.rule == "hush-chrome"


def test_a_notification_no_rule_matches_is_recorded_with_a_reason() -> None:
    send = _Recorder()
    Narrator(_ruleset(_chrome()), send=send).handle(_note(app="Slack"))
    assert send.enqueues() == []
    [entry] = history.recent()
    assert entry.app == "Slack"
    assert entry.spoken is False
    assert entry.reason, "a skipped notification with no reason is undiagnosable"


def test_a_spoken_notification_is_recorded_as_spoken() -> None:
    Narrator(_ruleset(_chrome()), send=_Recorder()).handle(_note())
    [entry] = history.recent()
    assert entry.spoken is True
    assert entry.rule == "chrome"
    assert entry.body == "tomorrow?"


def test_a_burst_past_the_limit_is_dropped_rather_than_queued() -> None:
    """A group chat waking up must not become forty queued utterances."""
    send = _Recorder()
    clock = 1000.0
    narrator = Narrator(_ruleset(_chrome(), per_minute=3), send=send, now=lambda: clock)
    for _ in range(6):
        narrator.handle(_note())
    assert len(send.enqueues()) == 3
    dropped = [e for e in history.recent() if not e.spoken]
    assert len(dropped) == 3
    assert "3 a minute" in dropped[0].reason


def test_the_limit_recovers_once_the_minute_has_passed() -> None:
    send = _Recorder()
    clock = 1000.0
    narrator = Narrator(_ruleset(_chrome(), per_minute=2), send=send, now=lambda: clock)
    narrator.handle(_note())
    narrator.handle(_note())
    narrator.handle(_note())
    assert len(send.enqueues()) == 2
    clock += 61.0
    narrator.handle(_note())
    assert len(send.enqueues()) == 3


def test_a_daemon_that_refuses_the_text_is_recorded_as_not_spoken() -> None:
    send = _Recorder(failure="no daemon at /run/speakd.sock")
    lines: list[str] = []
    Narrator(_ruleset(_chrome()), send=send, log=lines.append).handle(_note())
    [entry] = history.recent()
    assert entry.spoken is False
    assert "no daemon" in entry.reason
    assert any("no daemon" in line for line in lines)


def test_one_notification_that_explodes_does_not_end_the_loop() -> None:
    """A failure on one notification must not cost every notification after it."""

    class _Exploding(_Recorder):
        def __call__(self, verb: str, source: str, payload: dict[str, object]) -> str | None:
            if "boom" in source:
                raise RuntimeError("the floor gave way")
            return super().__call__(verb, source, payload)

    send = _Exploding()
    lines: list[str] = []
    everything = rules.Rule(name="all", app="*", say="{summary}. {body}")
    narrator = Narrator(_ruleset(everything), send=send, log=lines.append)
    narrator.run([_note(app="Boom"), _note()])

    assert len(send.enqueues()) == 1, "the notification after the failure was lost"
    assert any("the floor gave way" in line for line in lines)


def test_run_handles_every_notification_it_is_given() -> None:
    send = _Recorder()
    Narrator(_ruleset(_chrome()), send=send).run([_note(), _note(summary="Pappa")])
    assert len(send.enqueues()) == 2


def test_a_malformed_rules_file_speaks_nothing_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falling back here would read aloud the app a broken rule meant to silence."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "speakd"
    config.mkdir()
    (config / "notifications.toml").write_text("[[rule]]\nname = 17\n", encoding="utf-8")
    lines: list[str] = []
    assert load_ruleset(log=lines.append) is None
    assert any("could not be read" in line for line in lines)


def test_a_missing_rules_file_falls_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent is not broken: the connector should do something on the day it is installed."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_ruleset(log=lambda _line: None) == rules.DEFAULT_RULES
