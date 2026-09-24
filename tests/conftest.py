"""Isolation every test in this suite needs, whether or not it knows it.

`Daemon.__init__` reads the persisted mute flag out of `speakd.state`, and the
mute and disable verbs write it. Without this fixture a test run reads -- and
can leave behind -- the state of the daemon the developer is actually using:
run `speakctl mute` once for real, and suites with no interest in muting start
constructing a silent daemon and failing for a reason nowhere in their own
source.

Autouse and unconditional, because the files that need it are not the files
that mention it. `test_cc_send.py`, `test_supervise.py`, `test_cc_follow.py`,
`test_transport.py` and `test_benchmark.py` all build a `Daemon` without ever
naming `speakd.state`, and a fixture each of them had to remember to request
would be a fixture one of them eventually forgot.

`SPEAKD_STATE_DIR` goes with it: `watermark.state_dir` honours that override
first, so the Claude Code client's watermarks and session registrations land
here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "speakd-state"))
    # And configuration, now that the daemon writes some: the HTTP transport's
    # token. A test must never mint one in the developer's own ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # `tests/test_cli_verbs.py` starts real `python -m speakd` subprocesses,
    # which inherit this. Without it every such test spawns a follower that
    # outlives its daemon's socket and tails the developer's own transcripts.
    monkeypatch.setenv("SPEAKD_NO_FOLLOWER", "1")
    # And the notification connector with it, for the sharper version of the
    # same reason: an escaped one does not merely tail a transcript, it reads
    # the developer's own email and messages out loud.
    monkeypatch.setenv("SPEAKD_NO_NOTIFY", "1")
