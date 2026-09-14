"""The plugin manifest has to stay loadable and in step with the hooks."""

import json
import subprocess as sp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "clients" / "claude-code"


def test_the_plugin_manifest_parses() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "speakd"


def test_every_hook_we_handle_is_registered() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"Stop", "PostToolUse", "Notification", "UserPromptSubmit"}


def test_every_registered_hook_runs_the_one_entry_point() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [
        entry["command"]
        for matchers in hooks.values()
        for matcher in matchers
        for entry in matcher["hooks"]
    ]
    assert commands, "no hook commands registered"
    for command in commands:
        assert "${CLAUDE_PLUGIN_ROOT}" in command
        assert "speakd-hook.sh" in command


def test_hooks_have_a_timeout_short_enough_not_to_stall_a_turn() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for matchers in hooks.values():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                assert entry["timeout"] <= 5


WRAPPER = ROOT / "hooks" / "speakd-hook.sh"

GARBAGE = "not json at all"
VALID = json.dumps({"session_id": "plugin-test", "hook_event_name": "PreCompact"})


def test_the_command_each_hook_names_actually_exists() -> None:
    """A manifest pointing at a script that is not there is the obvious rot."""
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for matchers in hooks.values():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                relative = entry["command"].split("${CLAUDE_PLUGIN_ROOT}/", 1)[1]
                assert (ROOT / relative).is_file(), f"{relative} is not in the plugin"


def _invoke(
    wrapper: Path, stdin: str, *, state: Path, path: str, home: Path
) -> "sp.CompletedProcess[str]":
    """Run the wrapper the way Claude Code does: bash, payload on stdin."""
    return sp.run(
        ["bash", str(wrapper)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": path,
            "HOME": str(home),
            "SPEAKD_STATE_DIR": str(state),
            # No daemon is running in the test environment, so every enqueue
            # fails; the point here is that it fails into the log, quietly.
            "XDG_RUNTIME_DIR": str(state / "run"),
        },
    )


def _stub(bin_dir: Path, marker: Path, exit_code: int = 0) -> None:
    """A `speakd-claude-hook` on PATH that only records that it was reached."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "speakd-claude-hook"
    stub.write_text(
        f"#!/usr/bin/env bash\ncat >/dev/null\necho reached >> {marker}\nexit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def test_the_wrapper_prefers_the_entry_point_on_path(tmp_path: Path) -> None:
    """PATH wins over the checkout's venv.

    Asserted with a stub rather than the real binary: with the real one both
    branches produce the same log, so a wrapper that had lost its `command
    -v` branch entirely would still look correct.
    """
    marker = tmp_path / "reached"
    _stub(tmp_path / "bin", marker)
    state = tmp_path / "state"
    done = _invoke(
        WRAPPER,
        GARBAGE,
        state=state,
        path=f"{tmp_path / 'bin'}:/usr/bin:/bin",
        home=tmp_path / "home",
    )
    assert done.returncode == 0
    assert done.stdout == "", f"the wrapper printed {done.stdout!r}"
    assert marker.read_text(encoding="utf-8") == "reached\n"
    assert not (state / "claude-code" / "hook.log").exists(), "the venv copy ran as well"


def test_an_entry_point_that_fails_does_not_fail_the_turn(tmp_path: Path) -> None:
    """`|| true` is the whole reason a broken install is survivable."""
    marker = tmp_path / "reached"
    _stub(tmp_path / "bin", marker, exit_code=17)
    done = _invoke(
        WRAPPER,
        GARBAGE,
        state=tmp_path / "state",
        path=f"{tmp_path / 'bin'}:/usr/bin:/bin",
        home=tmp_path / "home",
    )
    assert marker.is_file(), "the stub never ran"
    assert done.returncode == 0, "a failing entry point reached Claude Code as a hook failure"
    assert done.stdout == ""


def test_the_wrapper_falls_back_to_the_checkout_venv(tmp_path: Path) -> None:
    """With nothing on PATH, the plugin still has to find its own venv.

    This is the ordinary case: Claude Code runs hooks with its own
    environment, not the shell the user activated the venv in.
    """
    state = tmp_path / "state"
    done = _invoke(WRAPPER, GARBAGE, state=state, path="/usr/bin:/bin", home=tmp_path / "home")
    assert done.returncode == 0
    assert done.stdout == ""
    log = state / "claude-code" / "hook.log"
    assert log.is_file(), "the venv fallback never resolved"
    assert "not json at all" in log.read_text(encoding="utf-8")


def test_a_well_formed_payload_leaves_the_log_alone(tmp_path: Path) -> None:
    """An event we do not handle is not a diagnostic. Silence, and exit 0."""
    state = tmp_path / "state"
    done = _invoke(WRAPPER, VALID, state=state, path="/usr/bin:/bin", home=tmp_path / "home")
    assert done.returncode == 0
    assert done.stdout == ""
    log = state / "claude-code" / "hook.log"
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_the_wrapper_exits_zero_when_speakd_is_not_installed_at_all(tmp_path: Path) -> None:
    """Uninstalling speakd must not break every turn of every session."""
    stranded = tmp_path / "elsewhere" / "hooks"
    stranded.mkdir(parents=True)
    copy = stranded / "speakd-hook.sh"
    copy.write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    done = _invoke(
        copy,
        GARBAGE,
        state=tmp_path / "state",
        path="/usr/bin:/bin",
        home=tmp_path / "home",
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert not (tmp_path / "state").exists()


def test_the_wrapper_stays_silent_and_zero_however_often_it_is_run(tmp_path: Path) -> None:
    """Claude Code fires this once per tool call. It must not drift."""
    state = tmp_path / "state"
    results = [
        _invoke(WRAPPER, VALID, state=state, path="/usr/bin:/bin", home=tmp_path / "home")
        for _ in range(5)
    ]
    assert [done.returncode for done in results] == [0] * 5
    assert {done.stdout for done in results} == {""}


MARKETPLACE = ROOT.parent.parent / ".claude-plugin" / "marketplace.json"


def test_the_marketplace_entry_points_at_the_plugin_we_ship() -> None:
    """`/plugin marketplace add <checkout>` is the documented install.

    It resolves a marketplace manifest at the repo root and follows each
    entry's `source` to a plugin. A README promising that command while the
    source pointed nowhere would fail at the first step a user takes, which
    is exactly what happened before this test existed.
    """
    repo = MARKETPLACE.parent.parent
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert "speakd" in entries, "the marketplace does not offer the plugin"
    source = (repo / entries["speakd"]["source"]).resolve()
    assert source == ROOT.resolve(), f"the marketplace points at {source}, not {ROOT}"
    manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == entries["speakd"]["name"]
