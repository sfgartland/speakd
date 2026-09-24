// speakd as a voice in Zotero's Read Aloud catalogue.
//
// Zotero's remote interface answers `getVoices` with voices keyed by tier
// (internals §2.2). speakd joins the `standard` tier as one voice for every
// language: sentence-granular, so the highlight is a sentence, and with no
// credits, so Zotero counts it as unlimited. Zotero's own voices stay beside
// it, so a user's own Read Aloud keeps working in a reader the plugin has
// adopted.
//
// Pure: reader-takeover.ts clones what these return into the reader.

/** speakd's voice id in Zotero's catalogue. Unprefixed, as remote voice ids are. */
export const SPEAKD_VOICE_ID = "speakd";

export interface CatalogueEntry {
  voices: Record<string, { label: string }>;
  locales: Record<string, string[]>;
  segmentGranularity: "sentence";
}

export interface VoicesAnswer {
  voices: Record<string, unknown[]>;
  standardCreditsRemaining: number | null;
  premiumCreditsRemaining: number | null;
  devMode: boolean;
}

export function speakdCatalogueEntry(): CatalogueEntry {
  // `*` matches any document language. No `creditsPerMinute`: Zotero then
  // reports no minutes remaining to run out of.
  return {
    voices: { [SPEAKD_VOICE_ID]: { label: "speakd" } },
    locales: { "*": [SPEAKD_VOICE_ID] },
    segmentGranularity: "sentence",
  };
}

function creditsOf(value: unknown): number | null {
  // Explicitly null, never undefined: Zotero copies anything !== null and
  // would do sums with an undefined.
  return typeof value === "number" ? value : null;
}

/**
 * Zotero's own `getVoices` answer (or null when there is none), with speakd
 * added when `offerSpeakd`. Without it, Zotero's answer is passed through as
 * it came: a reader the plugin refused is left as Zotero made it.
 */
export function mergeVoices(native: unknown, offerSpeakd: boolean): unknown {
  if (!offerSpeakd) return native;
  const answer = typeof native === "object" && native !== null ? (native as Record<string, unknown>) : {};
  const nativeVoices =
    typeof answer.error !== "string" && typeof answer.voices === "object" && answer.voices !== null
      ? (answer.voices as Record<string, unknown>)
      : {};
  const voices: Record<string, unknown[]> = {};
  for (const [tier, entries] of Object.entries(nativeVoices)) {
    if (Array.isArray(entries)) voices[tier] = [...entries];
  }
  voices.standard = [...(voices.standard ?? []), speakdCatalogueEntry()];
  return {
    voices,
    standardCreditsRemaining: creditsOf(answer.standardCreditsRemaining),
    premiumCreditsRemaining: creditsOf(answer.premiumCreditsRemaining),
    devMode: answer.devMode === true,
  } satisfies VoicesAnswer;
}

/**
 * A WAV of `seconds` of digital silence: what Zotero plays as the "sample"
 * when speakd is picked in its voice list. Zotero never plays speakd's
 * speech itself; speakd does.
 */
export function silentWav(sampleRate = 8000, seconds = 0.1): Uint8Array {
  const samples = Math.round(sampleRate * seconds);
  const bytes = new Uint8Array(44 + samples * 2);
  const view = new DataView(bytes.buffer);
  const text = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index++) view.setUint8(offset + index, value.charCodeAt(index));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  view.setUint32(40, samples * 2, true);
  return bytes;
}
