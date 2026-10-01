"""Codex's user-level installation and launchers."""

import json
import os
import shutil
import subprocess as sp
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "clients" / "codex"


def _codex_stub(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "codex"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$CODEX_ARGS"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _install(tmp_path: Path, client: Path = CLIENT) -> tuple[sp.CompletedProcess[str], Path, Path]:
    codex_home = tmp_path / "codex-home"
    args = tmp_path / "codex-args"
    bin_dir = tmp_path / "bin"
    _codex_stub(bin_dir)
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "CODEX_ARGS": str(args),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    result = sp.run(["bash", str(client / "install.sh")], env=env, capture_output=True, text=True)
    return result, codex_home, args


def test_installer_preserves_other_hooks_and_registers_mcp(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    existing = {
        "description": "Keep me",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]},
    }
    (codex_home / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "existing"\n', encoding="utf-8")

    done, home, args = _install(tmp_path)

    assert done.returncode == 0, done.stderr
    assert (home / "config.toml").read_text(encoding="utf-8") == 'model = "existing"\n'
    assert args.read_text(encoding="utf-8").splitlines() == [
        "mcp",
        "add",
        "speakd",
        "--",
        "bash",
        str((CLIENT / "speakd-mcp.sh").resolve()),
    ]
    installed: dict[str, Any] = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    assert installed["description"] == "Keep me"
    assert installed["hooks"]["Stop"][0] == {
        "hooks": [{"type": "command", "command": "echo other"}]
    }
    assert set(installed["hooks"]) == {"Stop", "Interrupt", "PermissionRequest", "UserPromptSubmit"}
    assert len(installed["hooks"]["Stop"]) == 2
    limits = {"UserPromptSubmit": 3, "Interrupt": 3, "PermissionRequest": 5, "Stop": 5}
    for event, limit in limits.items():
        handler = installed["hooks"][event][-1]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["timeout"] <= limit
        assert handler["command"] == f"bash '{(CLIENT / 'hooks' / 'speakd-hook.sh').resolve()}'"


def test_installer_is_idempotent_with_spaced_checkout_path(tmp_path: Path) -> None:
    client = tmp_path / "speakd checkout" / "clients" / "codex"
    client.mkdir(parents=True)
    for name in ("install.sh", "speakd-mcp.sh"):
        shutil.copyfile(CLIENT / name, client / name)
    (client / "hooks").mkdir()
    shutil.copyfile(CLIENT / "hooks" / "speakd-hook.sh", client / "hooks" / "speakd-hook.sh")

    first, home, _ = _install(tmp_path, client)
    second, _, _ = _install(tmp_path, client)

    assert first.returncode == second.returncode == 0
    hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert all(len(hooks[event]) == 1 for event in hooks)
    command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    payload = '{"hook_event_name":"UnsupportedEvent"}'
    result = sp.run(
        ["sh", "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SPEAKD_HOME": str(tmp_path / "missing"),
            "SPEAKD_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_installer_refuses_malformed_json_without_mutating_it(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    hooks = codex_home / "hooks.json"
    hooks.write_text("broken {", encoding="utf-8")

    done, _, args = _install(tmp_path)

    assert done.returncode != 0
    assert hooks.read_text(encoding="utf-8") == "broken {"
    assert not args.exists()


def test_mcp_launcher_answers_ping_from_checkout_venv() -> None:
    result = sp.run(
        ["bash", str(CLIENT / "speakd-mcp.sh")],
        input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": "/usr/bin:/bin", "SPEAKD_HOME": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == {}


def test_hook_launcher_preserves_context_and_logs_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    entry = bin_dir / "speakd-codex-hook"
    entry.write_text(
        '#!/usr/bin/env bash\ncat >/dev/null\nprintf "briefing context\\n"\n'
        'printf "diagnostic\\n" >&2\nexit 7\n',
        encoding="utf-8",
    )
    entry.chmod(0o755)
    state = tmp_path / "state"

    done = sp.run(
        ["bash", str(CLIENT / "hooks" / "speakd-hook.sh")],
        input='{"hook_event_name":"UserPromptSubmit"}',
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "SPEAKD_STATE_DIR": str(state)},
    )

    assert done.returncode == 0
    assert done.stdout == "briefing context\n"
    assert done.stderr == ""
    assert "diagnostic" in (state / "codex" / "hook.log").read_text(encoding="utf-8")
