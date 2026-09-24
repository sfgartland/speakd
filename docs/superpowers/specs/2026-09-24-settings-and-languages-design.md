# Typed settings that plugins declare, and speech in more than one language

Date: 2026-09-24. Status: designed in conversation, approved; builds on the
Zotero reader branch.

## Intent

- **One place to configure speakd.** Settings are typed and declared, not
  scattered across files and environment variables. The daemon itself, its
  in-process plugins, and its clients (the Zotero plugin, the Claude Code
  integration) each declare the settings they own. All of them appear in one
  Settings view in the speakd window, and a change reaches whoever owns the
  setting at once.
- **Text is spoken in its own language.** Today every word goes through
  Kokoro's American-English pipeline, so a French paper is read with English
  pronunciation. Kokoro 0.9.4 speaks nine languages (American and British
  English, Spanish, French, Hindi, Italian, Brazilian Portuguese, Japanese,
  Mandarin), and all nine should be used.
- **Where the language comes from.** The client says it when it knows (Zotero
  knows each document's language), and otherwise the daemon detects it per
  utterance. That covers AI agents, which may switch language mid-session.
- **Languages Kokoro cannot speak are handled deliberately.** German is the
  first case. The Zotero plugin steps aside and lets Zotero's own Read Aloud
  read it with a system voice. For other channels a setting decides whether
  such text is spoken badly or declined.

Out of scope: a second engine such as Piper for German, which is its own
project. Moving `profiles.toml` and `notifications.toml` into the new system,
which is later work. Choosing a voice per channel from the window.

## 1. Settings

**Declaration.** A setting has:
- `key`: `<owner>.<name>`, where the owner is `speech`, `http`, a plugin name,
  or a client name such as `zotero` or `claude-code`
- `type`: one of
  - `bool`
  - `int` / `float` with optional `min`, `max` and `step`
  - `string` with optional `multiline`
  - `choice` with `options`
  - `voice`: a choice among the engine's voices
  - `voice_map`: language code to voice
- `default`, `label`, `help`
- an optional `restart: true` for the few that only take effect on restart

**Declarers.**
- Core declares its settings at start.
- In-process plugins use `PluginContext.setting(name, type, default, ...)`,
  undone on dispose like every other registration.
- Clients use the new verb `declare_settings {settings: [...]}`, under an owner
  equal to their client name. The daemon persists a client's schema, so its
  settings still show, and can still be edited, while it is not connected.

**Store.** `~/.config/speakd/settings.toml`:
- One table per owner. Values only; schemas are persisted separately in
  `settings-schema.json`, which is the daemon's to write.
- Every write is validated against the declared type and bounds.
- A key with no declaration is kept untouched, so a value set for a plugin that
  is not loaded today survives.
- Invalid values on disk fall back to the default, with a warning.
- Written atomically: temp file, then rename.

**Verbs.**
- `settings {owner?}` returns `{schema: [...], values: {key: value}}`.
- `set_setting {key, value}` validates, persists, publishes the event
  `setting {key, value}` and answers `{value}`.
- `declare_settings {settings}` validates the declarations themselves and
  answers `{declared: n}`.

**Scoping.**
- Over the Unix socket every owner is reachable, since that is the user's own
  CLI and window.
- Over HTTP a client may declare, read and set only its own owner's keys; the
  owner is the source id's prefix up to the first `:`, so `zotero:K` is
  `zotero`. `speech.*` is readable but not settable over HTTP.
- `speakctl set <key> <value>` and `speakctl settings [owner]` work over the
  socket.

**Window.**
- A Settings view, reached from a gear in the title bar.
- Sections per owner: core first, then plugins, then clients.
- One control per type:
  - toggle, number with range, text / text area, select
  - voice select, populated from the engine
  - language→voice table
- A value changes on commit (blur or enter). The event updates every open view.
- The Tauri bridge forwards `settings` and `set_setting`, never
  `declare_settings`.

## 2. Languages

**Codes.** Lower-case BCP-47 primary tags, with a region only where Kokoro
distinguishes one: `en`, `en-gb`, `es`, `fr`, `hi`, `it`, `pt-br`, `ja`, `zh`.
`en` means American English, and `pt` normalises to `pt-br`. They map to
Kokoro's lang codes `a b e f h i p j z`.

**Resolution per utterance**, first match wins:
1. `enqueue` payload `lang`
2. the channel's language, set with the verb `set_language {lang}` (or
   `speakctl lang <code> --source …`, where `auto` clears it)
3. detection, when `speech.detect_language` is on (the default)
4. `speech.default_language`, default `en`

**Detection.**
- `lingua-language-detector`, imported lazily on first use; `test_import_cost`
  must stay green.
- Built for the supported languages plus German, Dutch, Swedish, Norwegian,
  Danish and Polish, so that common unsupported languages are recognised as
  such rather than guessed as English.
- Utterances shorter than 20 letters are not detected, since detection on them
  is noise; they use the channel's or the default language.
- A new `speakd[lang]` extra; without it detection is off and a single log line
  says why.

**Engine.**
- `Synthesizer.synthesize(text, voice, speed, lang)`. `lang` is the resolved
  code; FakeEngine and LazyEngine pass it through.
- KokoroEngine keeps a `KPipeline` per Kokoro lang code, created on first use,
  sharing one model. KPipeline accepts `model=` to share it; if this Kokoro
  version cannot share, pipelines are created per language anyway and the
  memory cost is documented.
- `ja` and `zh` count as supported only when their G2P extras import (misaki's
  `ja`/`zh`).
- `supported_languages() -> list[str]`.

**Voices.** The setting `speech.voices` (`voice_map`) has these defaults:

| code | voice |
|---|---|
| `en` | `af_heart` |
| `en-gb` | `bf_emma` |
| `es` | `ef_dora` |
| `fr` | `ff_siwis` |
| `hi` | `hf_alpha` |
| `it` | `if_sara` |
| `pt-br` | `pf_dora` |
| `ja` | `jf_alpha` |
| `zh` | `zf_xiaobei` |

The profile's own voice is used when it belongs to the resolved language
(Kokoro voices are prefixed by language: `a`/`b`/`e`/`f`/…); otherwise the
map's voice is used.

**Unsupported languages.** The setting `speech.unsupported_language` is a
choice:
- `default` (the default): speak with `speech.default_language`'s pipeline and
  voice, and publish `language {requested, used, reason:"unsupported"}` once
  per utterance.
- `decline`: decline with reason `no voice for <code>`.

`status` gains `languages: {supported: [...], default, detect}`, and
`started` gains `lang`.

## 3. Clients

**Zotero.**
- Every enqueue carries the document's language, `manager._lang`, normalised.
- Before taking over a read, the plugin checks `status.languages.supported`. If
  the document's language is not supported, it does not take over: Zotero's
  own Read Aloud proceeds with its voices, and the bar says
  "speakd has no <Language> voice; Zotero is reading".
- The plugin declares `zotero.*` settings: `first_section_segments` (int, 1–5,
  default 2) and `section_chars` (int, 600–4000, default 1800), and reads them
  from `settings {owner:"zotero"}` plus `setting` events.
- Port and token stay in Zotero's preferences, since they are what connects
  it.

**Claude Code.**
- `brief` gains an optional `lang`.
- The follower sends none, so detection applies.
- `speakd-mcp` declares `claude-code.briefing_guide` (a multiline string whose
  default is the built-in guide) and reads the guide from settings rather than
  `briefing.md`. An existing `briefing.md` is migrated into the setting once
  (read, set, renamed to `briefing.md.migrated`).

**Notifications.** No change; they get detection like every other channel.

## Order of work

1. The settings core: types, store, registry, verbs, CLI, and the window view.
2. Languages in the daemon: resolution, detection, engine, voices, unsupported
   languages, and `status`.
3. Clients: Zotero (language, stepping aside, declared settings) and Claude
   Code (`brief` lang, the guide setting).

Each phase is independently useful and ends with a green CI.

## Testing

- **Settings:** type validation for every type and bound; persistence
  round-trip; unknown keys kept; invalid-on-disk falls back; atomic write;
  `declare_settings` validation; the HTTP owner scoping (a `zotero:` client
  setting `speech.*` → refused); event on set; plugin settings undone on
  dispose.
- **Languages:** the resolution order; detection on and off; the short-text
  rule; `supported_languages` with and without the `ja`/`zh` extras (faked);
  voice choice (profile voice kept when it matches); both unsupported-language
  modes; a pipeline created once per language (a fake KPipeline factory).
- **Window:** a browser check on the simulation, which gains settings: every
  control type renders and commits, and a `setting` event updates the view.
- **Live:** a French sample PDF in the test Zotero is read with `lang: fr`; a
  German one makes the plugin step aside; the real Kokoro speaks a French
  sentence with `ff_siwis` (a test that needs the kokoro extra, skipped
  without it).
