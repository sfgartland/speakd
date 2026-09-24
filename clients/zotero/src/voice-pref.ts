// Zotero's per-language voice choice, `extensions.zotero.reader.readAloudVoices`,
// held for the length of the plugin's own read.
//
// Two things write it while speakd reads, and both are undone afterwards:
//   - A user who has never used Read Aloud has no entry at all, and Zotero
//     then shows its first-run voice dialog instead of reading (internals
//     §5). So before the read, the document's language is given speakd as
//     its voice, when it has none.
//   - Selecting speakd's voice persists it for the language (B:82286-82300).
//
// The value from before the first write is what is put back, byte for byte,
// or the pref cleared when it had none.
//
// Pure: reader-takeover.ts hands in Zotero's prefs.

export interface PrefStore {
  get(): unknown;
  set(value: string): void;
  clear(): void;
}

function baseOf(lang: string): string {
  return lang.toLowerCase().split(/[-_]/)[0] ?? "";
}

/**
 * The pref's new value with speakd as `lang`'s voice, or null when `lang`
 * already has a voice -- exactly, or by its base language, as Zotero
 * resolves it (`resolveLanguage`). An unreadable value counts as none.
 */
export function seededVoices(raw: unknown, lang: string, voiceId: string): string | null {
  let voices: Record<string, unknown> = {};
  if (typeof raw === "string" && raw !== "") {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        voices = parsed as Record<string, unknown>;
      }
    } catch {
      // Zotero reads it as {} too (X:1721-1727).
    }
  }
  const base = baseOf(lang);
  if (Object.keys(voices).some((key) => key === lang || baseOf(key) === base)) return null;
  return JSON.stringify({ ...voices, [lang]: { voice: voiceId, tierVoices: { standard: voiceId } } });
}

export class VoicePref {
  private readonly store: PrefStore;
  private saved: { value: unknown } | null = null;

  constructor(store: PrefStore) {
    this.store = store;
  }

  /** Remember the value as it stands, unless one is remembered already. */
  save(): void {
    if (this.saved === null) this.saved = { value: this.store.get() };
  }

  /** Give `lang` speakd's voice if it has none, remembering what was there. */
  seed(lang: string, voiceId: string): void {
    this.save();
    const seeded = seededVoices(this.store.get(), lang, voiceId);
    if (seeded !== null) this.store.set(seeded);
  }

  /** Put back what was there before the first save; nothing when nothing was saved. */
  restore(): void {
    if (this.saved === null) return;
    const { value } = this.saved;
    this.saved = null;
    if (typeof value === "string") this.store.set(value);
    else this.store.clear();
  }
}
