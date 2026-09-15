"""The hook's import cost, guarded.

`send.py` once reached into `cli.py` for `default_socket_path`, and `cli.py`
imports the synthesis pipeline, so every Claude Code tool call loaded numpy to
build a path -- about 200ms of a 250ms hook. Nothing visible breaks when that
comes back, which is why it is a test and not a comment.
"""

import subprocess
import sys


def _modules_after_importing(module: str) -> set[str]:
    """Import `module` in a clean interpreter and report what came with it.

    A subprocess rather than an assertion about this interpreter's
    `sys.modules`: pytest has already imported numpy by the time any test
    runs, so an in-process check can only ever pass.
    """
    source = f"import {module}, sys; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def test_the_hooks_send_path_does_not_import_numpy() -> None:
    loaded = _modules_after_importing("speakd.clients.send")
    assert "numpy" not in loaded


def test_the_hooks_send_path_does_not_import_the_pipeline() -> None:
    loaded = _modules_after_importing("speakd.clients.send")
    assert "speakd.pipeline" not in loaded
    assert "speakd.profiles" not in loaded
