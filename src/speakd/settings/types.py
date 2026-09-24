"""Setting declarations and validation, kept pure so nothing here touches disk.

A `Declaration` says what a setting is; `validate` says whether a value is
one. Both are used from `registry.py` at every boundary a value crosses --
loaded off disk, set over a verb, or given as a default -- so that "the store
holds an invalid value" cannot happen silently: it is either validated on the
way in, or replaced with the default and a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# owner: speech, http, render, a plugin name, or a client's own source id.
_OWNER_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# name: the part after the dot, e.g. "default_language".
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# A voice_map's keys: lower-case BCP-47 primary tags, with an optional region.
_LANG_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")

_TYPES = frozenset({"bool", "int", "float", "string", "choice", "voice", "voice_map"})


class SettingError(Exception):
    """A declaration or a value that does not obey its own rules."""


@dataclass(frozen=True)
class Declaration:
    key: str
    type: str
    default: object
    label: str
    help: str
    options: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    multiline: bool = False
    restart: bool = False


def _owner_key(owner: str, name: object) -> str:
    """The `<owner>.<name>` key, or the best key-ish string to name in an error.

    Called before `name` is known to be a string, so a bad name still gets a
    key that names *something* in the message rather than raising a second,
    unrelated exception while trying to report the first.
    """
    return f"{owner}.{name}"


def parse_declaration(owner: str, raw: dict[str, object]) -> Declaration:
    """Build one `Declaration` from a client, plugin or core's raw dict.

    Raises `SettingError` on anything that would make the setting unusable:
    an unknown type, a bad name, a choice with no options, `min > max`, or a
    default that does not itself validate -- a setting nobody can safely read
    the default of is not a setting.
    """
    name = raw.get("name")
    key = _owner_key(owner, name)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SettingError(f"{key}: a setting name must match {_NAME_RE.pattern}")
    setting_type = raw.get("type")
    if setting_type not in _TYPES:
        raise SettingError(f"{key}: unknown type {setting_type!r}")
    options_raw = raw.get("options")
    options: tuple[str, ...] | None = None
    if setting_type == "choice":
        if (
            not isinstance(options_raw, list)
            or not options_raw
            or not all(isinstance(o, str) for o in options_raw)
        ):
            raise SettingError(f"{key}: a choice needs a non-empty list of string 'options'")
        options = tuple(options_raw)
    elif options_raw is not None:
        # Given for a non-choice type: kept rather than refused, since a
        # client redeclaring across a type change should not also have to
        # scrub its old fields, but never trusted for validation below.
        pass
    minimum = raw.get("min")
    maximum = raw.get("max")
    if (
        minimum is not None
        and maximum is not None
        and isinstance(minimum, (int, float))
        and isinstance(maximum, (int, float))
        and minimum > maximum
    ):
        raise SettingError(f"{key}: min {minimum} is greater than max {maximum}")
    decl = Declaration(
        key=key,
        type=setting_type,
        default=raw.get("default"),
        label=str(raw.get("label", name)),
        help=str(raw.get("help", "")),
        options=options,
        min=minimum if isinstance(minimum, (int, float)) else None,
        max=maximum if isinstance(maximum, (int, float)) else None,
        step=raw.get("step") if isinstance(raw.get("step"), (int, float)) else None,
        multiline=bool(raw.get("multiline", False)),
        restart=bool(raw.get("restart", False)),
    )
    # Normalised (an int default for a float setting becomes a float) and
    # checked in the same step `set()` will use for every later value: a
    # default that could not itself pass this is not a fallback anything can
    # be trusted to land on.
    return replace(decl, default=validate(decl, decl.default))


def validate(decl: Declaration, value: object) -> object:
    """Normalise and check `value` against `decl`, or raise `SettingError`.

    Returns the value as it should be stored -- an `int` given for a `float`
    setting comes back as a `float`, and a `voice_map` comes back as a plain
    `dict[str, str]` -- so a caller never has to re-derive the stored shape.
    """
    key = decl.key
    if decl.type == "bool":
        if not isinstance(value, bool):
            raise SettingError(f"{key}: expected a bool, got {value!r}")
        return value
    if decl.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingError(f"{key}: expected an int, got {value!r}")
        return _bounded(key, value, decl.min, decl.max)
    if decl.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingError(f"{key}: expected a number, got {value!r}")
        return _bounded(key, float(value), decl.min, decl.max)
    if decl.type == "string":
        if not isinstance(value, str):
            raise SettingError(f"{key}: expected a string, got {value!r}")
        return value
    if decl.type == "choice":
        if not isinstance(value, str) or value not in (decl.options or ()):
            raise SettingError(f"{key}: {value!r} is not one of {list(decl.options or ())}")
        return value
    if decl.type == "voice":
        # Checking against the engine's real voice list is phase 2 (§2 of the
        # design); for now any non-empty string is accepted.
        if not isinstance(value, str) or not value:
            raise SettingError(f"{key}: a voice must be a non-empty string")
        return value
    if decl.type == "voice_map":
        if not isinstance(value, dict):
            raise SettingError(f"{key}: expected a map from language code to voice")
        result: dict[str, str] = {}
        for lang, voice in value.items():
            if not isinstance(lang, str) or not _LANG_RE.match(lang):
                raise SettingError(f"{key}: {lang!r} is not a language code ({_LANG_RE.pattern})")
            if not isinstance(voice, str) or not voice:
                raise SettingError(f"{key}: the voice for {lang!r} must be a non-empty string")
            result[lang] = voice
        return result
    raise SettingError(f"{key}: unknown type {decl.type!r}")  # pragma: no cover - guarded above


def _bounded(
    key: str, value: int | float, minimum: float | None, maximum: float | None
) -> int | float:
    if minimum is not None and value < minimum:
        raise SettingError(f"{key}: {value} is below the minimum {minimum}")
    if maximum is not None and value > maximum:
        raise SettingError(f"{key}: {value} is above the maximum {maximum}")
    return value


def to_json(decl: Declaration) -> dict[str, object]:
    """What the `settings` verb returns for one declaration."""
    return {
        "key": decl.key,
        "type": decl.type,
        "default": decl.default,
        "label": decl.label,
        "help": decl.help,
        "options": list(decl.options) if decl.options is not None else None,
        "min": decl.min,
        "max": decl.max,
        "step": decl.step,
        "multiline": decl.multiline,
        "restart": decl.restart,
    }
