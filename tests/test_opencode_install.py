"""OpenCode's user-level installation."""

import os
import subprocess as sp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _install(config_home: Path) -> sp.CompletedProcess[str]:
    env = {**os.environ, "XDG_CONFIG_HOME": str(config_home)}
    return sp.run(
        ["bash", str(ROOT / "clients" / "opencode" / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
    )


def test_installer_copies_audiobook_skill_and_replaces_it_on_rerun(tmp_path: Path) -> None:
    done = _install(tmp_path)
    assert done.returncode == 0, done.stderr
    skill = tmp_path / "opencode" / "skills" / "speakd-audiobook"
    assert (
        (skill / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: speakd-audiobook")
    )
    assert (skill / "scripts" / "prepare_text.py").is_file()
    assert not skill.is_symlink()

    (skill / "stale.txt").write_text("old", encoding="utf-8")
    assert _install(tmp_path).returncode == 0
    assert not (skill / "stale.txt").exists()
