"""The plugin manifest has to stay loadable and in step with the hooks."""

import json
import os
import subprocess as sp
from pathlib import Path

import pytest

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
                relative = entry["command"].split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].strip('"')
                assert (ROOT / relative).is_file(), f"{relative} is not in the plugin"


def _invoke(
    wrapper: Path,
    stdin: str,
    *,
    state: Path,
    path: str,
    home: Path,
    speakd_home: Path | None = None,
) -> "sp.CompletedProcess[str]":
    """Run the wrapper the way Claude Code does: bash, payload on stdin."""
    env = {
        "PATH": path,
        "HOME": str(home),
        "SPEAKD_STATE_DIR": str(state),
        # No daemon is running in the test environment, so every enqueue
        # fails; the point here is that it fails into the log, quietly.
        "XDG_RUNTIME_DIR": str(state / "run"),
    }
    if speakd_home is not None:
        env["SPEAKD_HOME"] = str(speakd_home)
    return sp.run(
        ["bash", str(wrapper)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
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


def _installed_copy(tmp_path: Path) -> Path:
    """The plugin where Claude Code actually puts it.

    `/plugin install` copies the directory to
    `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, so
    ${CLAUDE_PLUGIN_ROOT} is nowhere near the checkout and the wrapper's
    `../../..` lands in the cache, where there is no venv.
    """
    cache = tmp_path / "claude" / "plugins" / "cache" / "speakd" / "speakd" / "0.0.1"
    (cache / "hooks").mkdir(parents=True)
    copy = cache / "hooks" / "speakd-hook.sh"
    copy.write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    copy.chmod(0o755)
    return copy


def test_an_installed_copy_finds_the_interpreter_through_speakd_home(tmp_path: Path) -> None:
    """The documented install copies the plugin out of the checkout.

    Nothing in the copied tree can point back at the venv on its own, so
    $SPEAKD_HOME is what carries the checkout's location across the copy.
    Without it this whole path is a silent no-op.
    """
    state = tmp_path / "state"
    done = _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
        speakd_home=ROOT.parent.parent,
    )
    assert done.returncode == 0
    assert done.stdout == ""
    log = state / "claude-code" / "hook.log"
    assert log.is_file(), "the installed copy never reached the entry point"
    assert "not json at all" in log.read_text(encoding="utf-8")


def test_an_installed_copy_that_finds_nothing_says_so_in_the_log(tmp_path: Path) -> None:
    """Exiting 0 is right. Exiting 0 in silence is the bug.

    A user who installs the plugin, starts the daemon and hears nothing is
    told to `tail` the log. If the wrapper never found the entry point there
    was no log at all, and the one diagnostic the README promises did not
    exist.
    """
    state = tmp_path / "state"
    done = _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
    )
    assert done.returncode == 0
    assert done.stdout == ""
    log = state / "claude-code" / "hook.log"
    assert log.is_file(), "the wrapper found nothing and said nothing"
    written = log.read_text(encoding="utf-8")
    assert "speakd-claude-hook" in written
    assert "SPEAKD_HOME" in written, "the log must name the way out"


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
    # It must still leave a trace. Silence here is indistinguishable, to the
    # person tailing the log, from a hook that is working perfectly.
    assert (tmp_path / "state" / "claude-code" / "hook.log").is_file()


def test_the_wrapper_logs_where_the_entry_point_would_have(tmp_path: Path) -> None:
    """The wrapper's idea of the state directory must match `watermark`'s.

    Two implementations of one path, in bash and in Python. A user tailing
    the file the README names has to see both.
    """
    from speakd.clients.claude_code.watermark import state_dir

    state = tmp_path / "state"
    _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
    )
    monkeyed = os.environ.get("SPEAKD_STATE_DIR")
    os.environ["SPEAKD_STATE_DIR"] = str(state)
    try:
        assert (state_dir() / "hook.log").is_file()
    finally:
        if monkeyed is None:
            del os.environ["SPEAKD_STATE_DIR"]
        else:
            os.environ["SPEAKD_STATE_DIR"] = monkeyed


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


def test_the_prompt_hushes_fit_inside_the_manifests_budget() -> None:
    """The arithmetic, checked against the manifest rather than remembered.

    `UserPromptSubmit` sends two hushes in sequence and has the shortest
    window of the four events. Either number can be changed by someone who
    is not thinking about the other; this is what notices.
    """
    from speakd.clients.claude_code.hook import HUSH_TIMEOUT

    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    budget = min(
        entry["timeout"] for matcher in hooks["UserPromptSubmit"] for entry in matcher["hooks"]
    )
    # Two hushes, one budget each -- which is only true because `send` shares
    # one deadline between connecting and waiting for the reply. Without that
    # the real ceiling is 4 * HUSH_TIMEOUT and this arithmetic models half of
    # it; test_connecting_and_replying_share_one_budget is what holds it.
    # Start-up measured at ~0.22s on the development machine, doubled here.
    startup_allowance = 0.5
    worst_case = 2 * HUSH_TIMEOUT + startup_allowance
    assert worst_case <= budget, (
        f"two hushes at {HUSH_TIMEOUT}s plus start-up is {worst_case}s, against a {budget}s budget"
    )


def test_a_checkout_path_with_a_space_does_not_break_every_hook() -> None:
    """`${CLAUDE_PLUGIN_ROOT}` has to be quoted in the command.

    Claude Code runs a hook command through a POSIX shell, which splits an
    unquoted expansion on whitespace: `bash /home/a b/plugin/hooks/x.sh`
    becomes `bash /home/a` and exits 127. Not a silent failure but the loud
    kind — a hook failure on every single event, for anyone whose checkout
    sits under a directory with a space in it.
    """
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [
        entry["command"]
        for matchers in hooks.values()
        for matcher in matchers
        for entry in matcher["hooks"]
    ]
    for command in commands:
        assert '"${CLAUDE_PLUGIN_ROOT}' in command, f"unquoted expansion in {command!r}"


def test_the_quoted_command_actually_runs_from_a_path_with_a_space(tmp_path: Path) -> None:
    """The manifest's own command string, run under a real shell."""
    root = tmp_path / "a directory with spaces" / "plugin"
    (root / "hooks").mkdir(parents=True)
    target = root / "hooks" / "speakd-hook.sh"
    target.write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    command = hooks["Stop"][0]["hooks"][0]["command"]
    done = sp.run(
        ["bash", "-c", command],
        input=GARBAGE,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PLUGIN_ROOT": str(root),
            "SPEAKD_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert done.returncode == 0, f"exit {done.returncode}: {done.stderr.strip()}"
    assert done.stdout == ""
    assert done.stderr == ""


def test_the_wrapper_caps_the_log_at_the_same_size_the_entry_point_does() -> None:
    """Two writers, one file. A cap only one of them honours is not a cap."""
    from speakd.clients.claude_code.hook import LOG_CAP_BYTES

    script = WRAPPER.read_text(encoding="utf-8")
    assert f"LOG_CAP_BYTES={LOG_CAP_BYTES}" in script, (
        f"the wrapper's cap has drifted from the entry point's {LOG_CAP_BYTES}"
    )


def test_the_wrapper_actually_truncates_an_oversized_log(tmp_path: Path) -> None:
    """Asserted by running it, not by reading the constant back."""
    from speakd.clients.claude_code.hook import LOG_CAP_BYTES

    state = tmp_path / "state"
    (state / "claude-code").mkdir(parents=True)
    log = state / "claude-code" / "hook.log"
    log.write_text("old noise\n" * (LOG_CAP_BYTES // 5), encoding="utf-8")
    assert log.stat().st_size > LOG_CAP_BYTES

    done = _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
    )
    assert done.returncode == 0
    assert log.stat().st_size < LOG_CAP_BYTES, "the wrapper appended to an already oversized log"
    assert "could not find speakd-claude-hook" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a chmod 000 file")
@pytest.mark.parametrize(
    ("name", "make_log"),
    [
        ("unwritable", "unwritable"),
        ("a directory", "directory"),
        ("unwritable and over the cap", "big-unwritable"),
    ],
)
def test_a_log_it_cannot_write_never_reaches_the_transcript(
    tmp_path: Path, name: str, make_log: str
) -> None:
    """The wrapper's own diagnostics must not become the user's problem.

    One `sudo claude` leaving a root-owned log, or a state directory on a
    mount that went read-only, and every redirect in `log` starts reporting
    to stderr — which Claude Code shows in the transcript, on every single
    hook. The shell opens a redirect before the command's own `2>/dev/null`
    can apply, so each one has to sit inside a group whose stderr is
    already discarded.
    """
    state = tmp_path / "state"
    directory = state / "claude-code"
    directory.mkdir(parents=True)
    log = directory / "hook.log"
    if make_log == "directory":
        log.mkdir()
    else:
        payload = "x" * (300_000 if make_log == "big-unwritable" else 10)
        log.write_text(payload, encoding="utf-8")
        log.chmod(0o400)
    try:
        done = _invoke(
            _installed_copy(tmp_path),
            GARBAGE,
            state=state,
            path="/usr/bin:/bin",
            home=tmp_path / "home",
        )
    finally:
        if make_log != "directory":
            log.chmod(0o600)
    assert done.returncode == 0, f"{name}: exit {done.returncode}"
    assert done.stdout == "", f"{name}: stdout {done.stdout!r}"
    assert done.stderr == "", f"{name}: stderr {done.stderr!r}"


def _broken_entry_point(directory: Path, body: str) -> Path:
    """An executable that exists, passes `[ -x ]`, and cannot be run."""
    bin_dir = directory / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    entry = bin_dir / "speakd-claude-hook"
    entry.write_text(body, encoding="utf-8")
    entry.chmod(0o755)
    return entry


@pytest.mark.parametrize(
    ("name", "body"),
    [
        # A venv rebuilt on a Python that has since gone, or a checkout moved
        # after `uv sync` wrote absolute shebangs into it. Exit 127.
        ("a stale shebang", "#!/nonexistent/python3.11\nprint('never reached')\n"),
        # Exit 126: the file is executable, the interpreter is not.
        ("an unusable interpreter", "#!/etc/hostname\n"),
    ],
)
def test_an_entry_point_that_cannot_run_says_so_in_the_log(
    tmp_path: Path, name: str, body: str
) -> None:
    """`[ -x ]` is not the same question as "will this run".

    A stale shebang exits 127 with "bad interpreter" on stderr, which lands
    on Claude Code's transcript, while the log — the one place the README
    sends a user to look — stays empty. That is the Critical from the last
    round exactly, one layer in: the user has nothing to tail.
    """
    state = tmp_path / "state"
    _broken_entry_point(tmp_path / "speakd", body)
    done = _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
        speakd_home=tmp_path / "speakd",
    )
    assert done.returncode == 0, f"{name}: exit {done.returncode}"
    assert done.stdout == "", f"{name}: stdout {done.stdout!r}"
    assert done.stderr == "", f"{name}: stderr leaked to the transcript: {done.stderr!r}"
    log = state / "claude-code" / "hook.log"
    assert log.is_file(), f"{name}: the wrapper failed and said nothing"
    written = log.read_text(encoding="utf-8")
    assert "speakd-claude-hook" in written, f"{name}: the log does not name what failed"
    assert "uv sync" in written, f"{name}: the log does not say what to do about it"


def test_an_entry_point_that_writes_to_stderr_is_logged_not_leaked(tmp_path: Path) -> None:
    """Anything the entry point says on stderr belongs in the log.

    It writes nothing there today. If it ever does — a warning from a
    dependency, say — the transcript is the wrong place for it to appear.
    """
    state = tmp_path / "state"
    _broken_entry_point(
        tmp_path / "speakd",
        "#!/usr/bin/env bash\ncat >/dev/null\necho 'a dependency grumbled' >&2\nexit 0\n",
    )
    done = _invoke(
        _installed_copy(tmp_path),
        GARBAGE,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
        speakd_home=tmp_path / "speakd",
    )
    assert done.returncode == 0
    assert done.stderr == "", f"stderr leaked: {done.stderr!r}"
    assert "a dependency grumbled" in (state / "claude-code" / "hook.log").read_text(
        encoding="utf-8"
    )


def test_a_working_entry_point_logs_nothing_of_its_own(tmp_path: Path) -> None:
    """The quiet path stays quiet: no status line for a clean exit."""
    state = tmp_path / "state"
    _broken_entry_point(tmp_path / "speakd", "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n")
    done = _invoke(
        _installed_copy(tmp_path),
        VALID,
        state=state,
        path="/usr/bin:/bin",
        home=tmp_path / "home",
        speakd_home=tmp_path / "speakd",
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr == ""
    log = state / "claude-code" / "hook.log"
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_with_home_unset_both_halves_pick_the_same_log(tmp_path: Path) -> None:
    """`${HOME:-/tmp}` and `Path.home()` are not the same fallback.

    Python's `Path.home()` falls back to the passwd entry when HOME is
    unset; the shell's `${HOME:-/tmp}` fell back to /tmp. With HOME unset and
    no SPEAKD_STATE_DIR, the two halves of this client wrote their
    diagnostics to two different files, and whichever one the user tailed
    was missing half the story. Bash's own tilde expansion uses the passwd
    entry too, which is what makes the two agree.
    """
    probe = tmp_path / "probe.sh"
    # Source the wrapper's state_dir() without running the rest of it.
    body = WRAPPER.read_text(encoding="utf-8")
    body = body[: body.index("# Matches hook.LOG_CAP_BYTES")] + "state_dir\n"
    probe.write_text(body, encoding="utf-8")
    shell = sp.run(
        ["bash", str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},  # no HOME, no XDG_STATE_HOME
    )
    assert shell.returncode == 0, shell.stderr
    assert shell.stderr == ""

    python = sp.run(
        [
            str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"),
            "-c",
            "from speakd.clients.claude_code.watermark import state_dir; print(state_dir())",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert python.returncode == 0, python.stderr
    assert shell.stdout.strip() == python.stdout.strip(), (
        f"shell says {shell.stdout.strip()!r}, python says {python.stdout.strip()!r}"
    )
