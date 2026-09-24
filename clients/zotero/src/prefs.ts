// The plugin's preferences: where speakd listens, and the token it wants.
//
// Both are set in the preference pane (addon/content/preferences.xhtml); the
// defaults are in addon/prefs.js. The token is pasted in by the user, from
// `speakctl http-token`: the daemon's token file is not assumed readable
// from inside Zotero's flatpak.

import type { LinkConfig } from "./link";

export const PREF_PREFIX = "extensions.speakd-reader.";
export const PORT_PREF = `${PREF_PREFIX}port`;
export const TOKEN_PREF = `${PREF_PREFIX}token`;
export const DEFAULT_PORT = 8642;

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
