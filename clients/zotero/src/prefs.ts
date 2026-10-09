// The plugin's preferences: where speakd listens, and the token it wants.
//
// Both are set in the preference pane (addon/content/preferences.xhtml); the
// defaults are in addon/prefs.js. The token is pasted in by the user, from
// `speakctl http-token`: the daemon's token file is not assumed readable
// from inside Zotero's flatpak.

import { parseSettings, type ExportSettings } from "./export/dialog";
import { type FootnotePolicy } from "./footnotes";
import { type LlmConfig } from "./llm";
import type { LinkConfig } from "./link";
import { resolveProfile, type RequestProfile } from "./presets";

export const PREF_PREFIX = "extensions.speakd-reader.";
export const PORT_PREF = `${PREF_PREFIX}port`;
export const TOKEN_PREF = `${PREF_PREFIX}token`;
export const FOOTNOTE_POLICY_PREF = `${PREF_PREFIX}footnote_policy`;
export const LLM_ENABLED_PREF = `${PREF_PREFIX}llm_enabled`;
export const LLM_ENDPOINT_PREF = `${PREF_PREFIX}llm_endpoint`;
export const LLM_KEY_PREF = `${PREF_PREFIX}llm_key`;
export const LLM_MODEL_PREF = `${PREF_PREFIX}llm_model`;
/** The chosen provider preset. Empty on purpose: the stored endpoint decides
 * the request flavour until the user picks one, so an install from before
 * providers existed is not switched onto the opencode shape. */
export const LLM_PROVIDER_PREF = `${PREF_PREFIX}llm_provider`;
/** The request flags used when the provider is "custom". */
export const LLM_CUSTOM_SESSION_PREF = `${PREF_PREFIX}llm_custom_session`;
export const LLM_CUSTOM_THINKING_PREF = `${PREF_PREFIX}llm_custom_thinking`;
export const DEFAULT_PORT = 8642;
/** How a read treats footnotes (proposal §3, the brief's open question 1): drop the bare citations, read the rest at their section's end. */
export const DEFAULT_FOOTNOTE_POLICY: FootnotePolicy = "classify";
/** The one-line disclosure the preference pane carries when the LLM is on (reader-design revision #7). */
export const LLM_DISCLOSURE = "When enabled, this sends the paper's text to this endpoint.";

/** The link's configuration from the preferences' raw values, whatever the user typed. */
export function parseConfig(port: unknown, token: unknown): LinkConfig {
  const number = typeof port === "string" && port.trim() !== "" ? Number(port) : port;
  const valid = typeof number === "number" && Number.isInteger(number) && number > 0 && number < 65536;
  return {
    port: valid ? number : DEFAULT_PORT,
    token: typeof token === "string" ? token.trim() : "",
  };
}

/** The configuration as the preferences stand. */
export function readConfig(): LinkConfig {
  return parseConfig(Zotero.Prefs.get(PORT_PREF, true), Zotero.Prefs.get(TOKEN_PREF, true));
}

/** Call `onChange` whenever either preference changes. Returns an unsubscribe. */
export function observeConfig(onChange: () => void): () => void {
  const ids = [PORT_PREF, TOKEN_PREF].map((name) => Zotero.Prefs.registerObserver(name, onChange, true));
  return () => {
    for (const id of ids) Zotero.Prefs.unregisterObserver(id);
  };
}

/** The footnote policy as the preferences stand: the brief's open question 1, a setting rather than an architecture decision. */
export function readFootnotePolicy(): FootnotePolicy {
  return parseFootnotePolicy(Zotero.Prefs.get(FOOTNOTE_POLICY_PREF, true));
}

/** The policy as set, or the default for anything else. */
export function parseFootnotePolicy(value: unknown): FootnotePolicy {
  return value === "drop_all" || value === "defer_all" ? value : DEFAULT_FOOTNOTE_POLICY;
}

/**
 * The LLM's configuration as the preferences hold it (proposal §4.2): off
 * until an endpoint and a model are set, whatever the switch says. The key is
 * a string, masked in the pane, never logged.
 */
export function parseLlmConfig(values: {
  enabled?: unknown;
  endpoint?: unknown;
  key?: unknown;
  model?: unknown;
  provider?: unknown;
  customSessionHeader?: unknown;
  customThinking?: unknown;
}): LlmConfig {
  const endpoint = typeof values.endpoint === "string" ? values.endpoint.trim() : "";
  const model = typeof values.model === "string" ? values.model.trim() : "";
  const provider = typeof values.provider === "string" ? values.provider.trim() : "";
  const custom: RequestProfile = {
    sessionHeader: values.customSessionHeader === true,
    thinking: values.customThinking === true,
  };
  return {
    enabled: values.enabled === true && endpoint !== "" && model !== "",
    endpoint,
    apiKey: typeof values.key === "string" ? values.key.trim() : "",
    model,
    profile: resolveProfile(provider, endpoint, custom),
  };
}

/** The LLM's configuration as the preferences stand. */
export function readLlmConfig(): LlmConfig {
  const get = (name: string): unknown => Zotero.Prefs.get(name, true);
  return parseLlmConfig({
    enabled: get(LLM_ENABLED_PREF),
    endpoint: get(LLM_ENDPOINT_PREF),
    key: get(LLM_KEY_PREF),
    model: get(LLM_MODEL_PREF),
    provider: get(LLM_PROVIDER_PREF),
    customSessionHeader: get(LLM_CUSTOM_SESSION_PREF),
    customThinking: get(LLM_CUSTOM_THINKING_PREF),
  });
}

/** The audiobook export dialog's defaults, declared in addon/prefs.js as `export_*`. */
export function readExportSettings(): ExportSettings {
  const get = (name: string) => Zotero.Prefs.get(`${PREF_PREFIX}${name}`, true);
  return parseSettings({
    formatArticle: get("export_format_article"),
    formatBook: get("export_format_book"),
    destination: get("export_destination"),
    folder: get("export_folder"),
    skipReferences: get("export_skip_references"),
  });
}
