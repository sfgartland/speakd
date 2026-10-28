import { describe, expect, it } from "vitest";

import {
  CUSTOM_ID,
  GENERIC_PROFILE,
  OPENCODE_PROFILE,
  PRESETS,
  presetById,
  presetForBaseUrl,
  resolveProfile,
} from "../src/presets";

describe("PRESETS", () => {
  it("has unique ids, labels and base URLs", () => {
    expect(new Set(PRESETS.map((p) => p.id)).size).toBe(PRESETS.length);
    expect(new Set(PRESETS.map((p) => p.label)).size).toBe(PRESETS.length);
    const urls = PRESETS.filter((p) => p.baseUrl !== "").map((p) => p.baseUrl);
    expect(new Set(urls).size).toBe(urls.length);
    expect(PRESETS.every((p) => p.models.length === 0 || p.models.includes(p.defaultModel))).toBe(true);
  });

  it("includes the custom escape hatch", () => {
    expect(PRESETS.find((p) => p.id === CUSTOM_ID)).toBeDefined();
  });

  it("keeps opencode-go on the opencode request flavour", () => {
    expect(presetById("opencode")?.profile).toEqual(OPENCODE_PROFILE);
    expect(presetById("opencode")?.baseUrl).toBe("https://opencode.ai/zen/go/v1");
  });

  it("gives generic providers the plain flavour", () => {
    expect(presetById("openai")?.profile).toEqual(GENERIC_PROFILE);
    expect(presetById("ollama")?.profile).toEqual(GENERIC_PROFILE);
  });
});

describe("presetById", () => {
  it("finds a preset by id, and nothing for an unknown id", () => {
    expect(presetById("deepseek")?.label).toBe("DeepSeek");
    expect(presetById("nope")).toBeUndefined();
  });
});

describe("presetForBaseUrl", () => {
  it("matches exactly and ignores trailing slashes", () => {
    expect(presetForBaseUrl("https://api.openai.com/v1")?.id).toBe("openai");
    expect(presetForBaseUrl("https://api.openai.com/v1/")?.id).toBe("openai");
    expect(presetForBaseUrl("  https://api.deepseek.com  ")?.id).toBe("deepseek");
  });

  it("returns nothing for an unknown endpoint", () => {
    expect(presetForBaseUrl("https://example.com/v1")).toBeUndefined();
    expect(presetForBaseUrl("")).toBeUndefined();
  });
});

describe("resolveProfile", () => {
  const custom = { sessionHeader: true, thinking: true };

  it("uses the named preset's flavour", () => {
    expect(resolveProfile("openai", "https://example.com/v1", custom)).toEqual(GENERIC_PROFILE);
    expect(resolveProfile("opencode", "https://example.com/v1", custom)).toEqual(OPENCODE_PROFILE);
  });

  it("uses the custom flags when the provider is custom", () => {
    expect(resolveProfile(CUSTOM_ID, "https://example.com/v1", custom)).toEqual(custom);
    expect(resolveProfile(CUSTOM_ID, "", custom)).toEqual(custom);
  });

  it("falls back to the base URL when there is no provider id (existing installs)", () => {
    expect(resolveProfile("", "https://opencode.ai/zen/go/v1", custom)).toEqual(OPENCODE_PROFILE);
    expect(resolveProfile("", "https://api.openai.com/v1", custom)).toEqual(GENERIC_PROFILE);
  });

  it("uses the custom flags for an unknown endpoint with no provider id", () => {
    expect(resolveProfile("", "https://example.com/v1", custom)).toEqual(custom);
  });
});
