import { describe, expect, it } from "vitest";
import xhtml from "../addon/content/preferences.xhtml?raw";
import { GENERIC_PROFILE, OPENCODE_PROFILE } from "../src/presets";
import {
  DEFAULT_FOOTNOTE_POLICY,
  DEFAULT_PORT,
  FOOTNOTE_POLICY_PREF,
  LLM_CUSTOM_SESSION_PREF,
  LLM_CUSTOM_THINKING_PREF,
  LLM_DISCLOSURE,
  LLM_ENABLED_PREF,
  LLM_ENDPOINT_PREF,
  LLM_KEY_PREF,
  LLM_MODEL_PREF,
  LLM_PROVIDER_PREF,
  parseConfig,
  parseFootnotePlacement,
  parseFootnotePolicy,
  FOOTNOTE_PLACEMENT_PREF,
  parseLlmConfig,
} from "../src/prefs";

describe("parseConfig", () => {
  it("takes the port and token as set", () => {
    expect(parseConfig(8743, "abc")).toEqual({ port: 8743, token: "abc" });
    expect(parseConfig("8743", "abc")).toEqual({ port: 8743, token: "abc" });
  });

  it("falls back to speakd's default port for anything that is not one", () => {
    for (const port of [undefined, "", "http", 0, 70000, 1.5, -1]) {
      expect(parseConfig(port, "t").port).toBe(DEFAULT_PORT);
    }
    expect(DEFAULT_PORT).toBe(8642);
  });

  it("trims a pasted token, and treats a missing one as empty", () => {
    expect(parseConfig(8642, "  abc\n").token).toBe("abc");
    expect(parseConfig(8642, undefined).token).toBe("");
  });
});

describe("parseFootnotePolicy", () => {
  it("takes the three policies, and defaults to classify", () => {
    expect(parseFootnotePolicy("classify")).toBe("classify");
    expect(parseFootnotePolicy("drop_all")).toBe("drop_all");
    expect(parseFootnotePolicy("keep_all")).toBe("keep_all");
    for (const value of [undefined, null, "", "inline", "drop", 3]) {
      expect(parseFootnotePolicy(value)).toBe("classify");
    }
    expect(DEFAULT_FOOTNOTE_POLICY).toBe("classify");
  });

  it("still reads the old name defer_all as keep_all", () => {
    expect(parseFootnotePolicy("defer_all")).toBe("keep_all");
  });

  it("declares the pref beside the plugin's other settings", () => {
    expect(FOOTNOTE_POLICY_PREF).toBe("extensions.speakd-reader.footnote_policy");
  });
});

describe("parseFootnotePlacement", () => {
  it("takes the four placements, and defaults to after-paragraph", () => {
    for (const value of ["after-paragraph", "inline", "page-end", "chapter-end"]) {
      expect(parseFootnotePlacement(value)).toBe(value);
    }
    for (const value of [undefined, null, "", "end", 3]) {
      expect(parseFootnotePlacement(value)).toBe("after-paragraph");
    }
    expect(FOOTNOTE_PLACEMENT_PREF).toBe("extensions.speakd-reader.footnote_placement");
  });
});

describe("parseLlmConfig", () => {
  it("reads endpoint, key, model and the on/off switch; off until endpoint and model are set", () => {
    expect(
      parseLlmConfig({
        enabled: true,
        endpoint: "https://openrouter.ai/api/v1",
        key: "sk-x",
        model: "openai/gpt-4o-mini",
      }),
    ).toEqual({
      enabled: true,
      endpoint: "https://openrouter.ai/api/v1",
      apiKey: "sk-x",
      model: "openai/gpt-4o-mini",
      profile: GENERIC_PROFILE,
    });
    // Anything unset or empty falls back to off and empty strings.
    expect(parseLlmConfig({})).toEqual({
      enabled: false,
      endpoint: "",
      apiKey: "",
      model: "",
      profile: GENERIC_PROFILE,
    });
    expect(parseLlmConfig({ enabled: true, endpoint: "", model: "m" }).enabled).toBe(false);
    expect(parseLlmConfig({ enabled: true, endpoint: "e", model: "" }).enabled).toBe(false);
    expect(parseLlmConfig({ enabled: "yes", endpoint: "e", model: "m" }).enabled).toBe(false);
  });

  it("derives the opencode flavour from a stored URL when no provider is set (old installs)", () => {
    const config = parseLlmConfig({ enabled: true, endpoint: "https://opencode.ai/zen/go/v1", model: "m" });
    expect(config.profile).toEqual(OPENCODE_PROFILE);
  });

  it("uses the custom flags when the provider is custom", () => {
    const config = parseLlmConfig({
      enabled: true,
      endpoint: "https://example.com/v1",
      model: "m",
      provider: "custom",
      customSessionHeader: true,
      customThinking: false,
    });
    expect(config.profile).toEqual({ sessionHeader: true, thinking: false });
  });

  it("declares the seven prefs beside the plugin's other settings", () => {
    expect(LLM_ENABLED_PREF).toBe("extensions.speakd-reader.llm_enabled");
    expect(LLM_ENDPOINT_PREF).toBe("extensions.speakd-reader.llm_endpoint");
    expect(LLM_KEY_PREF).toBe("extensions.speakd-reader.llm_key");
    expect(LLM_MODEL_PREF).toBe("extensions.speakd-reader.llm_model");
    expect(LLM_PROVIDER_PREF).toBe("extensions.speakd-reader.llm_provider");
    expect(LLM_CUSTOM_SESSION_PREF).toBe("extensions.speakd-reader.llm_custom_session");
    expect(LLM_CUSTOM_THINKING_PREF).toBe("extensions.speakd-reader.llm_custom_thinking");
  });

  it("carries the disclosure line that enabled text goes to the endpoint", () => {
    expect(LLM_DISCLOSURE).toContain("sends the paper's text to this endpoint");
  });
});

describe("the preference pane", () => {
  it("binds the footnote policy and the LLM prefs that use a preference attribute", () => {
    for (const pref of [
      FOOTNOTE_POLICY_PREF,
      LLM_ENABLED_PREF,
      LLM_ENDPOINT_PREF,
      LLM_KEY_PREF,
      LLM_MODEL_PREF,
      LLM_CUSTOM_SESSION_PREF,
      LLM_CUSTOM_THINKING_PREF,
    ]) {
      expect(xhtml).toContain(pref);
    }
  });

  it("carries the provider picker and wires the pane's onload hook", () => {
    // The provider pref is written by the pane's JS (see llm-prefs.ts), not by
    // a preference attribute, so only the control has to be here.
    expect(LLM_PROVIDER_PREF).toBe("extensions.speakd-reader.llm_provider");
    expect(xhtml).toContain("id=\"speakd-reader-llm-provider\"");
    expect(xhtml).toContain("Zotero.SpeakdReader?.onPrefsEvent?.('load'");
  });

  it("carries the disclosure line the constant promises", () => {
    expect(xhtml).toContain(LLM_DISCLOSURE);
  });
});
