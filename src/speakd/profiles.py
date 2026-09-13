"""Profiles: a named ordered chain of transforms, plus how to say them.

A profile names transforms; the plugin host knows which exist. Resolution
reports names nobody provides rather than skipping them, because a profile that
silently does nothing is an afternoon spent wondering why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomllib

from speakd.plugins.host import PluginHost, RegisteredTransform


@dataclass(frozen=True)
class Profile:
    name: str
    transforms: tuple[str, ...] = ()
    voice: str = "af_heart"
    speed: float = 1.1
    interrupt_on: tuple[str, ...] = field(default=())


DEFAULT_PROFILE = Profile(name="default", transforms=("markdown", "pronunciation"))


def load_profiles(path: Path) -> dict[str, Profile]:
    """Read profiles from TOML, always including the default."""
    profiles: dict[str, Profile] = {"default": DEFAULT_PROFILE}
    if not path.is_file():
        return profiles
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    for name, raw in data.get("profile", {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"profile {name!r}: must be a table, got {type(raw).__name__}")
        speed = float(raw.get("speed", DEFAULT_PROFILE.speed))
        if speed <= 0:
            raise ValueError(f"profile {name!r}: speed must be greater than 0, got {speed}")
        transforms = raw.get("transforms", [])
        if not isinstance(transforms, list):
            raise ValueError(
                f"profile {name!r}: transforms must be a list, got {type(transforms).__name__}"
            )
        interrupt_on = raw.get("interrupt_on", [])
        if not isinstance(interrupt_on, list):
            raise ValueError(
                f"profile {name!r}: interrupt_on must be a list, got {type(interrupt_on).__name__}"
            )
        profiles[name] = Profile(
            name=name,
            transforms=tuple(transforms),
            voice=str(raw.get("voice", DEFAULT_PROFILE.voice)),
            speed=speed,
            interrupt_on=tuple(interrupt_on),
        )
    return profiles


def resolve_chain(
    profile: Profile,
    host: PluginHost,
) -> tuple[list[RegisteredTransform], list[str]]:
    """Return the profile's transforms in order, plus the names nobody provides."""
    available = {t.name: t for t in host.transforms()}
    chain: list[RegisteredTransform] = []
    missing: list[str] = []
    for name in profile.transforms:
        transform = available.get(name)
        if transform is None:
            missing.append(name)
        else:
            chain.append(transform)
    return chain, missing
