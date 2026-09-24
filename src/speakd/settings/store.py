"""The settings files: a hand-written TOML values file, and a JSON schema file.

Standard library only (Global Constraint of the settings-core plan):
`tomllib` reads, and writing is a small serializer of our own, because the
only value shapes that ever reach it are `bool`, `int`, `float`, `str` and a
`dict[str, str]` (a `voice_map`). Every write re-reads the file first and is
atomic -- a temp file in the same directory, then `os.replace` -- so a
setting changed by hand while the daemon runs is not clobbered, and a writer
that dies mid-write leaves either the old file or the new one, never a
half-written one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import tomllib

# owner.name -> value, where value is bool | int | float | str | dict[str, str].
Values = dict[str, object]

# owner -> list of raw declaration dicts, exactly as `declare_settings` and
# `PluginContext.setting` gave them.
Schema = dict[str, list[dict[str, object]]]


def _warn(message: str) -> None:
    """One stderr line. A daemon that will not start over a bad settings file
    is a worse failure than one that starts with defaults and says so."""
    print(f"speakd: {message}", file=sys.stderr)


def _escape_toml_string(value: str) -> str:
    """A basic TOML string literal for `value`, escaped so it round-trips.

    `\\n` as a two-character escape inside a single-line quoted string is
    valid TOML and `tomllib` unescapes it back to a real newline -- simpler
    than emitting a triple-quoted literal string and worrying about a
    triple-quote delimiter appearing inside the text.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _escape_toml_string(value)
    raise TypeError(f"not a settings value: {value!r}")


def _toml_value(value: object) -> str:
    if isinstance(value, dict):
        # A voice_map: the only mapping type a setting ever holds. Written as
        # an inline table so no nested `[owner.name]` section is needed.
        pairs = ", ".join(f"{_escape_toml_string(k)} = {_toml_scalar(v)}" for k, v in value.items())
        return f"{{ {pairs} }}"
    return _toml_scalar(value)


def _render_toml(tree: dict[str, dict[str, object]]) -> str:
    """One `[owner]` table per owner, in a stable order so diffs stay small."""
    lines: list[str] = []
    for owner in sorted(tree):
        table = tree[owner]
        if not table:
            continue
        lines.append(f"[{owner}]")
        for name in sorted(table):
            lines.append(f"{name} = {_toml_value(table[name])}")
        lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        # The write failed partway; leave nothing half-finished behind for
        # the next read to trip over.
        tmp.unlink(missing_ok=True)
        raise


class SettingsStore:
    def __init__(self, values_path: Path, schema_path: Path) -> None:
        self.values_path = values_path
        self.schema_path = schema_path

    # ---- values (settings.toml) ----

    def _load_tree(self) -> dict[str, dict[str, object]]:
        """The raw per-owner tables, or empty (with a warning) if unreadable."""
        try:
            raw = self.values_path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            _warn(f"{self.values_path} is not valid TOML ({exc}); starting from an empty file")
            return {}
        tree: dict[str, dict[str, object]] = {}
        for owner, table in parsed.items():
            if isinstance(table, dict):
                tree[owner] = dict(table)
        return tree

    def load_values(self) -> Values:
        values: Values = {}
        for owner, table in self._load_tree().items():
            for name, value in table.items():
                values[f"{owner}.{name}"] = value
        return values

    def write_value(self, key: str, value: object) -> None:
        """Set one key, having re-read the file so a hand edit is not lost."""
        owner, _, name = key.partition(".")
        tree = self._load_tree()
        tree.setdefault(owner, {})[name] = value
        _atomic_write(self.values_path, _render_toml(tree))

    # ---- schema (settings-schema.json) ----

    def load_schema(self) -> Schema:
        try:
            raw = self.schema_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _warn(f"{self.schema_path} is not valid JSON ({exc}); starting from an empty schema")
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {owner: raws for owner, raws in parsed.items() if isinstance(raws, list)}

    def write_schema(self, owner: str, raws: list[dict[str, object]]) -> None:
        """Replace `owner`'s persisted declarations, keeping every other owner's."""
        schema = self.load_schema()
        schema[owner] = raws
        _atomic_write(
            self.schema_path,
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
        )
