import { describe, expect, it } from "vitest";
import xhtml from "../addon/content/preferences.xhtml?raw";
import {
  DEFAULT_FOOTNOTE_POLICY,
  DEFAULT_PORT,
  FOOTNOTE_POLICY_PREF,
  LLM_DISCLOSURE,
  LLM_ENABLED_PREF,
  LLM_ENDPOINT_PREF,
  LLM_KEY_PREF,
  LLM_MODEL_PREF,
  parseConfig,
  parseFootnotePolicy,
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
    expect(parseFootnotePolicy("defer_all")).toBe("defer_all");
    for (const value of [undefined, null, "", "inline", "drop", 3]) {
      expect(parseFootnotePolicy(value)).toBe("classify");
    }
    expect(DEFAULT_FOOTNOTE_POLICY).toBe("classify");
  });

  it("declares the pref beside the plugin's other settings", () => {
    expect(FOOTNOTE_POLICY_PREF).toBe("extensions.speakd-reader.footnote_policy");
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
    ).toEqual({ enabled: true, endpoint: "https://openrouter.ai/api/v1", apiKey: "sk-x", model: "openai/gpt-4o-mini" });
    // Anything unset or empty falls back to off and empty strings.
    expect(parseLlmConfig({})).toEqual({ enabled: false, endpoint: "", apiKey: "", model: "" });
    expect(parseLlmConfig({ enabled: true, endpoint: "", model: "m" }).enabled).toBe(false);
    expect(parseLlmConfig({ enabled: true, endpoint: "e", model: "" }).enabled).toBe(false);
    expect(parseLlmConfig({ enabled: "yes", endpoint: "e", model: "m" }).enabled).toBe(false);
  });

  it("declares the four prefs beside the plugin's other settings", () => {
    expect(LLM_ENABLED_PREF).toBe("extensions.speakd-reader.llm_enabled");
    expect(LLM_ENDPOINT_PREF).toBe("extensions.speakd-reader.llm_endpoint");
    expect(LLM_KEY_PREF).toBe("extensions.speakd-reader.llm_key");
    expect(LLM_MODEL_PREF).toBe("extensions.speakd-reader.llm_model");
  });

  it("carries the disclosure line that enabled text goes to the endpoint", () => {
    expect(LLM_DISCLOSURE).toContain("sends the paper's text to this endpoint");
  });
});

describe("the preference pane", () => {
  it("binds the footnote policy and the four LLM prefs", () => {
    for (const pref of [
      FOOTNOTE_POLICY_PREF,
      LLM_ENABLED_PREF,
      LLM_ENDPOINT_PREF,
      LLM_KEY_PREF,
      LLM_MODEL_PREF,
    ]) {
      expect(xhtml).toContain(pref);
    }
  });

  it("carries the disclosure line the constant promises", () => {
    expect(xhtml).toContain(LLM_DISCLOSURE);
  });
});
