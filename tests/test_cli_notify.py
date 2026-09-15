"""`speakctl notify` — the tooling that makes the rules writable.

What an app calls itself in a notification cannot be looked up; it has to be
observed. These subcommands are how, so their failure modes matter as much as
their successes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from speakd import cli
from speakd.clients.notifications import history
from speakd.clients.notifications.starter import STARTER_TOML


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _config(tmp_path: Path) -> Path:
    return tmp_path / "config" / "speakd" / "notifications.toml"


def test_init_writes_a_starter_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["notify", "init"]) == 0
    assert _config(tmp_path).read_text(encoding="utf-8") == STARTER_TOML
    assert str(_config(tmp_path)) in capsys.readouterr().out


def test_init_refuses_to_overwrite_a_file_you_have_edited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# mine\n", encoding="utf-8")
    assert cli.main(["notify", "init"]) == 1
    assert path.read_text(encoding="utf-8") == "# mine\n"
    assert "--force" in capsys.readouterr().err


def test_init_force_overwrites(tmp_path: Path) -> None:
    path = _config(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# mine\n", encoding="utf-8")
    assert cli.main(["notify", "init", "--force"]) == 0
    assert path.read_text(encoding="utf-8") == STARTER_TOML


def test_test_reports_what_the_rules_would_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["notify", "init"])
    capsys.readouterr()  # drop init's line so the JSON is all that is left
    assert cli.main(["notify", "test", "Google Chrome", "Mamma", "tomorrow?"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["speak"] is True
    assert verdict["rule"] == "chrome"
    assert "Mamma" in verdict["text"]
    assert verdict["channel"] == "notify:google-chrome"


def test_test_reports_a_notification_no_rule_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["notify", "init"])
    capsys.readouterr()  # drop init's line so the JSON is all that is left
    assert cli.main(["notify", "test", "Steam", "Sale", "half price"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["speak"] is False
    assert verdict["reason"]


def test_test_rejects_an_unknown_urgency(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["notify", "test", "App", "--urgency", "urgent"]) == 2
    assert "critical" in capsys.readouterr().err


def test_a_malformed_rules_file_is_reported_rather_than_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[[rule]]\nname = 17\n", encoding="utf-8")
    assert cli.main(["notify", "test", "Google Chrome"]) == 2
    assert str(path) in capsys.readouterr().err


def test_recent_on_an_empty_history_says_why_it_might_be_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ "Nothing here" is useless; the likeliest cause is that nothing ever ran."""
    assert cli.main(["notify", "recent"]) == 0
    assert "connector" in capsys.readouterr().out


def test_recent_prints_what_arrived(capsys: pytest.CaptureFixture[str]) -> None:
    history.record(
        history.Entry(
            when=1789474326.0,
            app="Google Chrome",
            summary="Mamma",
            body="are we still on?",
            spoken=True,
            rule="chrome",
        )
    )
    history.record(
        history.Entry(
            when=1789474330.0,
            app="Steam",
            summary="Sale",
            body="half price",
            spoken=False,
            reason="no rule matched",
        )
    )
    assert cli.main(["notify", "recent"]) == 0
    out = capsys.readouterr().out
    assert "Google Chrome" in out and "chrome" in out
    assert "Steam" in out and "no rule matched" in out
    assert "spoken" in out and "skipped" in out


def test_recent_honours_its_limit(capsys: pytest.CaptureFixture[str]) -> None:
    for index in range(5):
        history.record(
            history.Entry(when=float(index), app=f"app-{index}", summary="", body="", spoken=True)
        )
    assert cli.main(["notify", "recent", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "app-4" in out and "app-3" in out
    assert "app-0" not in out


def test_recent_rejects_a_limit_that_is_not_a_number(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["notify", "recent", "--limit", "lots"]) == 2
    assert "integer" in capsys.readouterr().err


def test_recent_clear_forgets_everything(capsys: pytest.CaptureFixture[str]) -> None:
    history.record(history.Entry(when=1.0, app="Google Chrome", summary="", body="", spoken=True))
    assert cli.main(["notify", "recent", "--clear"]) == 0
    assert history.recent() == []


def test_tap_reports_a_missing_busctl_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from speakd.clients.notifications import monitor

    def absent(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("busctl")

    monkeypatch.setattr(monitor, "stream", absent)
    cli.main(["notify", "init"])
    assert cli.main(["notify", "tap"]) == 1
    assert "busctl" in capsys.readouterr().err


def test_notify_with_no_subcommand_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["notify"]) == 2
    assert "usage" in capsys.readouterr().err.lower()
