import { describe, expect, it } from "vitest";
import {
  DEFAULT_FOOTNOTE_POLICY,
  DEFAULT_PORT,
  FOOTNOTE_POLICY_PREF,
  parseConfig,
  parseFootnotePolicy,
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
