# Settings Core — Implementation Plan (phase 1 of settings-and-languages)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. TDD throughout: every test is written first and watched failing before the code.

**Goal:** Typed settings that core, in-process plugins and clients declare, stored in one file, served by verbs and events, editable from `speakctl` and a Settings view in the window.

**Spec:** `docs/superpowers/specs/2026-09-24-settings-and-languages-design.md` §1 (binding). §2–3 (languages, clients) are later phases; do not build them here.

**Architecture:** a new package `src/speakd/settings/`:
- `types.py`: setting declarations and validation, pure functions.
- `store.py`: the TOML file and the persisted schema.
- `registry.py`: declarations by owner, values, and change callbacks.

The daemon owns one `Settings` object and exposes it through three verbs: `settings`, `set_setting` and `declare_settings`. `PluginContext.setting()` declares through it. Only the transports and the window change beyond that.

## Global Constraints

- **Standard library only.** `tomllib` reads; writing TOML is a small serializer of our own, because values are only bool, int, float, str and a map from str to str. Writes are atomic: a temp file in the same directory, then `os.replace`.
- **Files:**
  - `$XDG_CONFIG_HOME/speakd/settings.toml`, one table per owner, holding values only.
  - `$XDG_CONFIG_HOME/speakd/settings-schema.json`, the declarations persisted for offline clients.
- **Keys are `<owner>.<name>`.**
  - owner matches `[a-z][a-z0-9-]*`; name matches `[a-z][a-z0-9_]*`.
  - Core owners: `speech`, `http`, `render`.
  - A client's owner is its source id up to the first `:` (`zotero:K` → `zotero`, `claude-code:x` → `claude-code`).
- **Types:** `bool`, `int`, `float` (`min`, `max`, `step`, all optional), `string` (`multiline`), `choice` (`options` is a non-empty list of strings), `voice`, and `voice_map` (a map from language code to voice).
  - `voice` validates as a non-empty string for now; checking against the engine's list is phase 2.
  - Every declaration has `default`, `label`, `help` and an optional `restart: bool`.
- **Values:**
  - Validation errors are refused with a message naming the key and the rule.
  - A key with no declaration is preserved on disk untouched, and `settings` does not list it.
  - An invalid value on disk falls back to the default, with a single stderr warning.
- **Over HTTP:**
  - `settings`, `set_setting` and `declare_settings` are allowed.
  - A client may declare and set only keys of its own owner.
  - It may read its own owner and `speech`.
  - `set_setting` on `speech.*` over HTTP is refused.
- **In the window:** the Tauri bridge forwards `settings` and `set_setting`, never `declare_settings`.

## Review Focus

- A client declaring a key outside its own owner should be refused, and nothing stored. Tested in Task 3.
- A client redeclaring its schema with a different type for an existing key: the new schema wins, and a stored value that no longer validates falls back to the default, with a warning. Tested in Task 2.
- A settings file edited by hand while the daemon runs: the next `set_setting` must not wipe the other keys. Re-read before writing, in Task 2.
- A setting's default must itself be valid for its declaration, or the declaration is refused. Tested in Task 1.
- `set_setting` with the right type but out of bounds (int above `max`) should be refused. Tested in Task 1.

### Task 1: types (`settings/types.py`)

- `Declaration` is a frozen dataclass with `key`, `type`, `default`, `label`, `help`, `options`, `min`, `max`, `step`, `multiline` and `restart`.
- `parse_declaration(owner, raw: dict) -> Declaration` builds the key from `owner` plus `raw["name"]`. It raises `SettingError` on:
  - an unknown type
  - a bad name
  - missing options for a choice
  - `min > max`
  - a default that fails `validate`
- `validate(decl, value) -> value` returns the value normalised; an int is accepted for a float. It raises `SettingError`:
  - `bool`: must be a real bool, not an int.
  - `int`: must not be a bool.
  - `choice`: must be one of the options.
  - `voice_map`: must be a dict from str to non-empty str, with language codes matching `^[a-z]{2}(-[a-z]{2})?$`.
- `to_json(decl) -> dict` is what `settings` returns per declaration.
- Tests in `tests/test_settings_types.py`: every type accepts and refuses correctly, including bounds; a bad default is refused; bool and int are not interchangeable.

### Task 2: store (`settings/store.py`)

- `SettingsStore(values_path, schema_path)` provides:
  - `load_values() -> dict[str, object]`, flat `owner.name` keys.
  - `write_value(key, value)`, which re-reads the file, sets the key, and writes it atomically.
  - `load_schema() -> dict[owner, list[raw]]` and `write_schema(owner, raws)`.
- The TOML writer must round-trip through `tomllib` for our value types, escaping strings properly (quotes, backslashes, newlines; multi-line strings use `"""` or escaped `\n`, either being fine as long as they round-trip).
- Tests in `tests/test_settings_store.py`:
  - round trips of every value type
  - a multiline string with quotes
  - an unknown key preserved across a write
  - a hand edit between two writes kept
  - the atomic write (no temp file left behind)
  - a corrupt file read as empty, with a warning, and not overwritten until the next write (which keeps valid keys where possible)

### Task 3: registry (`settings/registry.py`)

- `Settings(store)` provides:
  - `declare(owner, raws, *, persist: bool)`: replaces that owner's declarations. Clients use `persist=True`; core and plugins use False, since they redeclare at every start.
  - `undeclare(owner, names)`: for plugin disposal.
  - `schema(owner=None) -> list[dict]`.
  - `values(owner=None) -> dict`: effective values, falling back to defaults.
  - `get(key)` and `set(key, value) -> value`, where set validates and then persists.
  - `on_change(callback) -> Disposable`, with callbacks `(key, value)`.
- On construction it loads the persisted client schemas, so offline clients' settings exist. Declarations from the current run override persisted ones.
- Tests in `tests/test_settings_registry.py`:
  - defaults when nothing is stored
  - stored values win
  - a redeclaration with a changed type falls back to the default
  - an offline client's schema is loaded from disk
  - `on_change` fires on set, and not on a failed set

### Task 4: daemon verbs and core declarations

- Add `SETTINGS`, `SET_SETTING` and `DECLARE_SETTINGS` to `Verb`.
- `Daemon.__init__` takes `settings: Settings | None`. When it is None, the daemon builds one on a store under `$XDG_CONFIG_HOME/speakd`; tests pass their own.
- **Core declares** `speech.default_language` (choice over Kokoro's languages; default `en`) and `speech.detect_language` (bool, True). These are declared now but not yet used, which is phase 2.
- **Verb handlers:**
  - `settings {owner?}` answers `{schema, values}`.
  - `set_setting {key, value}` answers `{value}` and publishes `setting {key, value}`.
  - `declare_settings {settings: [...]}` uses the owner derived from the request's `source_id`, and refuses an empty owner. It answers `{declared: n}`.
- **Scoping.** The daemon verbs themselves are unscoped; scoping belongs to the transports (Task 5). The one exception is `declare_settings`, which always declares under the source's own owner, whichever transport it came from.
- **`PluginContext.setting(name, type, default, **fields)`** declares under the plugin's name and is undone on dispose. Add a test in `tests/test_plugin_host.py`.
- Tests in `tests/test_settings_verbs.py`: each verb; the event on set; a failed set publishes nothing; declare under the source's owner.

### Task 5: transports and CLI

- **HTTP** (`transport_http.py`): add the three verbs to `VERBS`, plus an owner check before the handler runs:
  - The owner is the source id up to the first `:`.
  - `declare_settings`: always allowed, since it is scoped by owner already.
  - `set_setting`: only when the key's owner equals the request's owner.
  - `settings`: an `owner` of the client's own or `speech` is allowed; no owner is answered with only those two.
  - Refusals return 200 `{ok:false, error}`.
- Tests in `tests/test_transport_http.py`: `zotero:K` sets `zotero.x` (ok), sets `speech.default_language` (refused), and declares `claude-code.y` (declared as `zotero.y` or refused; pick refused and state that in the test name).
- **CLI:**
  - `speakctl settings [owner]` prints the schema and values as a readable table (key, value, default, type).
  - `speakctl set <key> <value>` parses the value by the declared type: bool accepts `true/false/on/off/1/0`; `voice_map` accepts `fr=ff_siwis,it=if_sara`.
  - Tests in `tests/test_cli.py`, using the existing `_fake_call` pattern.
- **Tauri bridge:** in `clients/gui/app/src-tauri/src/bridge.rs`, add `settings` and `set_setting` to `FORWARDED`, with a doc line. Run `cargo build`.

### Task 6: the Settings view in the window

- `clients/gui/pinned.html`:
  - A gear button in the title bar toggles a Settings panel that replaces the text region while it is open.
  - Sections per owner: core owners first (`speech`, `http`, `render`), then the others alphabetically. Each section title is the owner, with a friendly name map for known owners.
  - One control per type:
    - `bool`: toggle
    - `int` / `float`: number input with min, max and step
    - `string`: input, or a textarea when multiline
    - `choice` and `voice`: select for choice, text input for voice for now
    - `voice_map`: a table of language → voice rows with add and remove
  - A value commits on change or blur through `set_setting`. An error is shown under the control, and the control reverts to the daemon's value.
  - A `setting` event updates the control, unless it has focus.
  - `restart` settings show "applies after restart".
  - Everything is built with `textContent`; no `innerHTML`.
- `clients/gui/shared/speakd-source.js`: `SimulatedSource` gains a small schema for the browser tab, covering one of each type across `speech` and `zotero`, and implements `settings` and `set_setting` with the same validation rules in miniature, emitting `setting`.
- **Browser check** (Playwright via `uv run --with playwright`, system Chrome, serving `clients/gui` over `python3 -m http.server`): open settings; every control type renders; changing a number commits and persists in the sim; an out-of-range value shows an error and reverts; a `setting` event updates an unfocused control. Keep the check script in `clients/gui/test-browser/settings_check.py`, a new directory, with a one-line README.

### Task 7: docs and gate

- A README section, "Settings".
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest -q` and `cargo build`. All must be green.
