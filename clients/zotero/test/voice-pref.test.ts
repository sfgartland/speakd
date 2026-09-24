import { describe, expect, it } from "vitest";
import { VoicePref, seededVoices } from "../src/voice-pref";

describe("seededVoices", () => {
  const SEED = { voice: "speakd", tierVoices: { standard: "speakd" } };

  it("seeds speakd for the document's language when the pref is unset, empty or unreadable", () => {
    for (const raw of [undefined, "", "{}", "not json", "[1]"]) {
      expect(JSON.parse(seededVoices(raw, "en", "speakd")!)).toEqual({ en: SEED });
    }
  });

  it("adds the language beside the voices the user chose for others", () => {
    const raw = JSON.stringify({ de: { voice: "anna", speed: 1.2 } });
    expect(JSON.parse(seededVoices(raw, "en", "speakd")!)).toEqual({ de: { voice: "anna", speed: 1.2 }, en: SEED });
  });

  it("leaves a language the user already has a voice for alone, regional or not", () => {
    expect(seededVoices(JSON.stringify({ en: { voice: "x" } }), "en", "speakd")).toBeNull();
    expect(seededVoices(JSON.stringify({ "en-GB": { voice: "x" } }), "en", "speakd")).toBeNull();
    expect(seededVoices(JSON.stringify({ en: { voice: "x" } }), "en-US", "speakd")).toBeNull();
  });
});

function store(initial: string | undefined) {
  const log: string[] = [];
  let value = initial;
  return {
    log,
    get value() {
      return value;
    },
    get: () => value,
    set: (next: string) => {
      log.push(`set ${next}`);
      value = next;
    },
    clear: () => {
      log.push("clear");
      value = undefined;
    },
  };
}

describe("VoicePref", () => {
  it("seeds a first-time user's pref, and clears it again on restore", () => {
    const prefs = store(undefined);
    const pref = new VoicePref(prefs);
    pref.seed("en", "speakd");
    expect(JSON.parse(prefs.value!)).toEqual({ en: { voice: "speakd", tierVoices: { standard: "speakd" } } });
    pref.restore();
    expect(prefs.value).toBeUndefined();
    pref.restore();
    expect(prefs.log).toHaveLength(2);
  });

  it("puts back exactly what was there before the first save, however often it is saved", () => {
    const before = JSON.stringify({ de: { voice: "anna" } });
    const prefs = store(before);
    const pref = new VoicePref(prefs);
    pref.seed("en", "speakd");
    pref.save();
    prefs.set("changed by selectVoice");
    pref.restore();
    expect(prefs.value).toBe(before);
  });

  it("changes nothing for a user with a voice for the language, until Zotero's own selection does", () => {
    const before = JSON.stringify({ en: { voice: "anna" } });
    const prefs = store(before);
    const pref = new VoicePref(prefs);
    pref.seed("en", "speakd");
    expect(prefs.log).toEqual([]);
    pref.restore();
    expect(prefs.value).toBe(before);
  });
});
