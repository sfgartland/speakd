"""Tests for profiles and chain resolution."""

from pathlib import Path

import pytest

from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import DEFAULT_PROFILE, Profile, load_profiles, resolve_chain


def test_default_profile_speaks_markdown_and_pronunciation() -> None:
    assert DEFAULT_PROFILE.transforms == ("markdown", "pronunciation")
    assert DEFAULT_PROFILE.voice == "af_heart"
    assert DEFAULT_PROFILE.speed == 1.1


def test_load_profiles_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        """
[profile.philosophy]
transforms = ["markdown", "citations"]
speed = 1.0

[profile.monitor]
transforms = ["markdown", "summarize"]
interrupt_on = ["error", "done"]
"""
    )
    profiles = load_profiles(path)
    assert profiles["philosophy"].transforms == ("markdown", "citations")
    assert profiles["philosophy"].speed == 1.0
    assert profiles["philosophy"].voice == "af_heart"
    assert profiles["monitor"].interrupt_on == ("error", "done")


def test_load_profiles_on_a_missing_file_yields_only_the_default(tmp_path: Path) -> None:
    profiles = load_profiles(tmp_path / "absent.toml")
    assert profiles == {"default": DEFAULT_PROFILE}


def test_load_profiles_rejects_a_non_positive_speed(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text("[profile.bad]\ntransforms = []\nspeed = 0\n")
    with pytest.raises(ValueError, match="speed"):
        load_profiles(path)


def test_load_profiles_rejects_a_bare_string_transforms(tmp_path: Path) -> None:
    # A missing pair of brackets is an easy typo: "markdown" without [] is a
    # string, and `tuple("markdown")` would silently iterate its characters
    # rather than error, hiding the mistake behind eight bogus "missing"
    # transform names later.
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\ntransforms = "markdown"\n')
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)


def test_load_profiles_rejects_a_bare_string_interrupt_on(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\ntransforms = ["markdown"]\ninterrupt_on = "error"\n')
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)


def test_load_profiles_rejects_a_scalar_transforms(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text("[profile.bad]\ntransforms = 5\n")
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)


def test_load_profiles_rejects_a_non_table_profile(tmp_path: Path) -> None:
    # `[profile]` with a bare key inside it (rather than a `[profile.name]`
    # sub-table) makes that key's value the "profile", which is a string,
    # not a table -- the loader must not reach for `.get` on it.
    path = tmp_path / "profiles.toml"
    path.write_text('[profile]\nxyz = "hello"\n')
    with pytest.raises(ValueError, match="xyz"):
        load_profiles(path)


def test_resolve_chain_orders_transforms_as_the_profile_names_them() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    profile = Profile(name="p", transforms=("pronunciation", "markdown"))
    chain, missing = resolve_chain(profile, host)
    assert [t.name for t in chain] == ["pronunciation", "markdown"]
    assert missing == []


def test_resolve_chain_reports_a_transform_nobody_provides() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    profile = Profile(name="p", transforms=("markdown", "citations"))
    chain, missing = resolve_chain(profile, host)
    assert [t.name for t in chain] == ["markdown"]
    assert missing == ["citations"]


def test_load_profiles_rejects_a_non_string_transform_element(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\ntransforms = [["markdown"], "pronunciation"]\n')
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)


def test_load_profiles_rejects_a_scalar_transform_element(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\ntransforms = ["markdown", 5]\n')
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)


def test_load_profiles_rejects_a_non_string_voice(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\nvoice = ["af_heart", "af_bella"]\n')
    with pytest.raises(ValueError, match="voice"):
        load_profiles(path)


def test_load_profiles_names_the_profile_when_speed_is_not_a_number(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text('[profile.bad]\nspeed = "fast"\n')
    with pytest.raises(ValueError, match="bad"):
        load_profiles(path)
