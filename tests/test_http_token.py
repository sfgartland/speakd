"""Tests for the bearer token the HTTP transport requires."""

import stat
from pathlib import Path

from speakd import http_token


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_token_is_made_once_and_kept_private(tmp_path: Path) -> None:
    path = tmp_path / "speakd" / "http-token"
    token = http_token.ensure(path)
    assert len(token) >= 40
    assert mode(path) == 0o600
    assert http_token.ensure(path) == token
    assert http_token.read(path) == token


def test_a_readable_token_file_is_made_private_again(tmp_path: Path) -> None:
    path = tmp_path / "http-token"
    token = http_token.ensure(path)
    path.chmod(0o644)
    assert http_token.ensure(path) == token
    assert mode(path) == 0o600


def test_an_empty_token_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "http-token"
    path.write_text("\n")
    assert len(http_token.ensure(path)) >= 40


def test_the_default_path_follows_xdg_config_home(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert http_token.token_path() == tmp_path / "speakd" / "http-token"


def test_speakctl_prints_it(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from speakd.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["http-token"]) == 0
    assert capsys.readouterr().out.strip() == http_token.read()
