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
    # Applied to each sentence unit after segmentation, so a unit keeps its own
    # source span however much the transform rewrites it.
    sentence_transforms: tuple[str, ...] = ()
    voice: str = "af_heart"
    speed: float = 1.1
    interrupt_on: tuple[str, ...] = field(default=())


DEFAULT_PROFILE = Profile(name="default", transforms=("markdown", "pronunciation"))

# A second voice, so that a notification is recognisable as one without
# looking at the screen -- which is the whole point of having it read out.
# `am_michael` rather than a fourth name: it is one of the three voices the
# model ships in this install, so the profile works offline on the day it is
# added.
#
# No `markdown` in the chain. A notification is not a markdown document, and
# that transform's block splitting, table markers and quote markers are all
# answers to a question a one-line message never asks.
NOTIFICATION_PROFILE = Profile(
    name="notification",
    transforms=("pronunciation",),
    voice="am_michael",
    speed=1.05,
)


# For documents whose reader highlights what is being spoken, the Zotero
# reader first. No transforms over the whole text: every rewriting transform
# marks its pieces inexact, which drops each sentence's span to the whole
# piece, and a highlight that lights a page is no use to follow. The reader
# cleans its text before enqueueing it instead, keeping its own map of where
# each character came from. Pronunciation runs per sentence instead, after
# segmentation: each unit already has its own span, and a rewrite leaves that
# span as the sentence's source range, which is exactly what the highlight needs.
PDF_PROFILE = Profile(name="pdf", transforms=(), sentence_transforms=("pronunciation",))


def _names(profile: str, raw: dict[str, object], key: str) -> tuple[str, ...]:
    """A profile's list of transform names, validated with the profile's name
    in every message."""
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"profile {profile!r}: {key} must be a list, got {type(value).__name__}")
    for element in value:
        if not isinstance(element, str):
            # resolve_chain looks each name up in a dict, so a nested list
            # surfaced there as "unhashable type: 'list'" -- no profile
            # name, no file, and a long way from the config that caused it.
            # A scalar is worse: it is hashable, so it became a nonsense
            # entry in `missing` and read as a transform nobody provides.
            raise ValueError(
                f"profile {profile!r}: {key} must be a list of strings, "
                f"got {type(element).__name__}"
            )
    return tuple(value)


def load_profiles(path: Path) -> dict[str, Profile]:
    """Read profiles from TOML, always including the built-in ones.

    Built in, not merely shipped: a `[profile.notification]` table in the
    file replaces this one entirely, so the voice and speed are a user's to
    change without having to know what the default chain was.
    """
    profiles: dict[str, Profile] = {
        "default": DEFAULT_PROFILE,
        "notification": NOTIFICATION_PROFILE,
        "pdf": PDF_PROFILE,
    }
    if not path.is_file():
        return profiles
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    for name, raw in data.get("profile", {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"profile {name!r}: must be a table, got {type(raw).__name__}")
        raw_speed = raw.get("speed", DEFAULT_PROFILE.speed)
        try:
            speed = float(raw_speed)
        except (TypeError, ValueError) as exc:
            # float() names the value but not the profile, and a config error
            # that cannot say which profile it came from is a config error you
            # go looking for.
            raise ValueError(
                f"profile {name!r}: speed must be a number, got {raw_speed!r}"
            ) from exc
        if speed <= 0:
            raise ValueError(f"profile {name!r}: speed must be greater than 0, got {speed}")
        transforms = _names(name, raw, "transforms")
        sentence_transforms = _names(name, raw, "sentence_transforms")
        voice = raw.get("voice", DEFAULT_PROFILE.voice)
        if not isinstance(voice, str):
            # str() would turn a list into "['a', 'b']" -- a plausible-looking
            # voice name that fails deep in synthesis rather than at load.
            raise ValueError(
                f"profile {name!r}: voice must be a string, got {type(voice).__name__}"
            )
        interrupt_on = raw.get("interrupt_on", [])
        if not isinstance(interrupt_on, list):
            raise ValueError(
                f"profile {name!r}: interrupt_on must be a list, got {type(interrupt_on).__name__}"
            )
        profiles[name] = Profile(
            name=name,
            transforms=transforms,
            sentence_transforms=sentence_transforms,
            voice=voice,
            speed=speed,
            interrupt_on=tuple(interrupt_on),
        )
    return profiles


def resolve_chain(
    profile: Profile,
    host: PluginHost,
    *,
    sentences: bool = False,
) -> tuple[list[RegisteredTransform], list[str]]:
    """Return the profile's transforms in order, plus the names nobody provides.

    `sentences` resolves the per-sentence chain instead of the whole-text one.
    """
    available = {t.name: t for t in host.transforms()}
    chain: list[RegisteredTransform] = []
    missing: list[str] = []
    for name in profile.sentence_transforms if sentences else profile.transforms:
        transform = available.get(name)
        if transform is None:
            missing.append(name)
        else:
            chain.append(transform)
    return chain, missing
