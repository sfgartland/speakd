"""A `secret` setting: stored privately, and never reported as anything but set or not.

Every way a value leaves the daemon -- the `settings` verb, `set_setting`'s
reply, the `setting` event, `speakctl` -- is checked here for the key itself.
"""

from __future__ import annotations

import io
import os
import stat
import threading
from pathlib import Path

import pytest

from speakd.channels import ChannelTable
from speakd.cli import main
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.settings.types import SettingError
from speakd.synth.fake import FakeEngine

KEY = "sk-or-v1-0123456789abcdef"
SETTING = "speech.openrouter_api_key"


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json")


@pytest.fixture
def settings(store: SettingsStore) -> Settings:
    s = Settings(store)
    s.declare(
        "speech",
        [{"name": "openrouter_api_key", "type": "secret", "default": "", "label": "Key"}],
        persist=False,
    )
    return s


# ---- the registry


def test_a_secret_is_reported_only_as_set_or_not(settings: Settings) -> None:
    heard: list[object] = []
    settings.on_change(lambda key, value: heard.append(value))
    assert settings.get(SETTING) is False
    assert settings.set(SETTING, KEY) is True
    assert settings.get(SETTING) is True
    assert settings.values()[SETTING] is True
    assert heard == [True]
    assert settings.secret(SETTING) == KEY
    assert settings.schema()[0]["default"] is False


def test_an_empty_value_clears_it(settings: Settings) -> None:
    settings.set(SETTING, KEY)
    assert settings.set(SETTING, "") is False
    assert settings.secret(SETTING) is None


def test_it_never_reaches_settings_toml_and_its_file_is_private(
    settings: Settings, store: SettingsStore
) -> None:
    settings.set(SETTING, f"  {KEY}\n")
    assert not store.values_path.exists() or KEY not in store.values_path.read_text()
    secrets = store.values_path.parent / "secrets"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700
    path = secrets / SETTING
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == KEY
    assert [p.name for p in secrets.iterdir()] == [SETTING], "no temp file left behind"


def test_a_refusal_never_echoes_the_value(settings: Settings) -> None:
    for bad in (f"{KEY} trailing words", f"{KEY}\x07", "x" * 5000):
        with pytest.raises(SettingError) as info:
            settings.set(SETTING, bad)
        assert KEY not in str(info.value) and "xxxx" not in str(info.value)
    assert settings.get(SETTING) is False


def test_secret_reads_only_declared_secrets(settings: Settings) -> None:
    settings.declare(
        "speech",
        [{"name": "plain", "type": "string", "default": "x", "label": "Plain"}],
        persist=False,
    )
    with pytest.raises(SettingError):
        settings.secret("speech.plain")


# ---- the daemon


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


@pytest.fixture
def daemon(store: SettingsStore, monkeypatch: pytest.MonkeyPatch) -> Daemon:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        settings=Settings(store),
    )


def set_key(d: Daemon, value: str) -> Response:
    return d.handle(
        Request(verb=Verb.SET_SETTING, source_id="", payload={"key": SETTING, "value": value})
    )


def test_no_verb_or_event_carries_the_key(daemon: Daemon) -> None:
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    daemon.bus.subscribe(record)
    reply = set_key(daemon, KEY)
    assert reply.ok and reply.data == {"value": True}
    listed = daemon.handle(Request(verb=Verb.SETTINGS, source_id="", payload={}))
    status = daemon.handle(Request(verb=Verb.STATUS, source_id="", payload={}))
    everything = repr([reply, listed, status, seen])
    assert KEY not in everything
    assert listed.data["values"][SETTING] is True  # type: ignore[index]
    assert [e.data for e in seen if e.kind == "setting"] == [{"key": SETTING, "value": True}]


def test_the_engine_uses_the_setting_and_the_environment_wins(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    def source() -> object:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id="", payload={}))
        return status.data["engine"]["openrouter"]["key_source"]  # type: ignore[index]

    assert not daemon.remote.has_key() and source() is None
    set_key(daemon, KEY)
    assert daemon._openrouter_key() == KEY and source() == "settings"
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert daemon._openrouter_key() == "from-env" and source() == "environment"


def test_choosing_openrouter_needs_a_key_and_entering_one_allows_it(daemon: Daemon) -> None:
    choose = Request(
        verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "openrouter"}
    )
    refused = daemon.handle(choose)
    assert not refused.ok and SETTING in str(refused.error)
    set_key(daemon, KEY)
    assert daemon.handle(choose).ok


# ---- speakctl


def fake_daemon(monkeypatch: pytest.MonkeyPatch) -> list[Request]:
    from speakd import cli

    calls: list[Request] = []
    schema = [{"key": SETTING, "type": "secret", "default": False}]

    def call(_socket: Path, request: Request) -> Response:
        calls.append(request)
        if request.verb is Verb.SETTINGS:
            return Response(ok=True, data={"schema": schema, "values": {SETTING: True}})
        return Response(ok=True, data={"value": bool(request.payload["value"])})

    monkeypatch.setattr(cli, "_call", call)
    return calls


def test_speakctl_reads_a_secret_from_stdin_not_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = fake_daemon(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["set", SETTING]) == 0
    assert calls[1].payload == {"key": SETTING, "value": KEY}
    out = capsys.readouterr()
    assert out.out.strip() == "set" and KEY not in out.out + out.err


def test_speakctl_warns_about_a_secret_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_daemon(monkeypatch)
    assert main(["set", SETTING, KEY]) == 0
    assert "visible in `ps`" in capsys.readouterr().err


def test_speakctl_settings_prints_set_not_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_daemon(monkeypatch)
    assert main(["settings"]) == 0
    line = next(row for row in capsys.readouterr().out.splitlines() if SETTING in row)
    assert " set " in f" {line} " and "not set" in line


def test_the_secrets_directory_is_tightened_if_it_was_loose(
    settings: Settings, store: SettingsStore
) -> None:
    secrets = store.values_path.parent / "secrets"
    secrets.mkdir(mode=0o755)
    os.chmod(secrets, 0o755)
    settings.set(SETTING, KEY)
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700
