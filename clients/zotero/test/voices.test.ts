import { describe, expect, it } from "vitest";
import { SPEAKD_VOICE_ID, mergeVoices, silentWav, speakdCatalogueEntry, type VoicesAnswer } from "../src/voices";

describe("mergeVoices", () => {
  it("offers speakd alone when Zotero has no voices of its own to offer", () => {
    for (const native of [null, { error: "network" }, { voices: null }, "nonsense"]) {
      expect(mergeVoices(native, true)).toEqual({
        voices: { standard: [speakdCatalogueEntry()] },
        standardCreditsRemaining: null,
        premiumCreditsRemaining: null,
        devMode: false,
      });
    }
  });

  it("adds speakd to Zotero's own voices, keeping them and their credits", () => {
    const zotero = { voices: { en: { label: "Zotero" } }, locales: { en: ["z"] }, segmentGranularity: "sentence" };
    const merged = <VoicesAnswer>mergeVoices(
      {
        voices: { standard: [zotero], premium: [zotero] },
        standardCreditsRemaining: 30,
        premiumCreditsRemaining: 0,
        devMode: true,
      },
      true,
    );
    expect(merged.voices).toEqual({ standard: [zotero, speakdCatalogueEntry()], premium: [zotero] });
    expect(merged.standardCreditsRemaining).toBe(30);
    expect(merged.premiumCreditsRemaining).toBe(0);
    expect(merged.devMode).toBe(true);
  });

  it("passes Zotero's answer through untouched when speakd is not to be offered", () => {
    const native = { error: "network" };
    expect(mergeVoices(native, false)).toBe(native);
  });

  it("describes speakd as a sentence-granular voice for any language, with no credits", () => {
    const entry = speakdCatalogueEntry();
    expect(entry).toEqual({
      voices: { [SPEAKD_VOICE_ID]: { label: "speakd" } },
      locales: { "*": [SPEAKD_VOICE_ID] },
      segmentGranularity: "sentence",
    });
    expect(entry).not.toHaveProperty("creditsPerMinute");
  });
});

describe("silentWav", () => {
  it("is a valid, silent 16-bit mono WAV", () => {
    const bytes = silentWav(8000, 0.1);
    const view = new DataView(bytes.buffer);
    const text = (offset: number) => String.fromCharCode(...bytes.slice(offset, offset + 4));
    expect(text(0)).toBe("RIFF");
    expect(text(8)).toBe("WAVE");
    expect(text(36)).toBe("data");
    expect(view.getUint32(24, true)).toBe(8000);
    expect(view.getUint32(40, true)).toBe(1600);
    expect(bytes.length).toBe(44 + 1600);
    expect(bytes.slice(44).every((byte) => byte === 0)).toBe(true);
  });
});
