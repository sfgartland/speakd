import { describe, expect, it } from "vitest";
import { estimateSeconds, formatEstimate } from "../../src/export/estimate";

describe("estimateSeconds", () => {
  it("divides characters by the speech rate at speed 1.0", () => {
    // 15 chars/s of audio, so 1500 characters is 100 seconds.
    expect(estimateSeconds(1500)).toBe(100);
  });

  it("divides by speed, since a faster voice makes shorter audio", () => {
    expect(estimateSeconds(1500, 2)).toBe(50);
    expect(estimateSeconds(1500, 0.5)).toBe(200);
  });

  it("multiplies by the real-time factor, since rendering itself takes time too", () => {
    expect(estimateSeconds(1500, 1, 3)).toBe(300);
  });

  it("defaults speed and rtf to 1", () => {
    expect(estimateSeconds(150)).toBe(estimateSeconds(150, 1, 1));
  });
});

describe("formatEstimate", () => {
  it("formats hours and minutes", () => {
    expect(formatEstimate(80 * 60)).toBe("about 1 h 20 min");
  });

  it("formats minutes alone under an hour", () => {
    expect(formatEstimate(45 * 60)).toBe("about 45 min");
  });

  it("formats seconds alone under a minute", () => {
    expect(formatEstimate(30)).toBe("about 30 s");
  });

  it("rounds to the nearest minute once past a minute", () => {
    expect(formatEstimate(65)).toBe("about 1 min");
  });

  it("drops a zero minutes remainder on an even number of hours", () => {
    expect(formatEstimate(2 * 3600)).toBe("about 2 h");
  });

  it("never reports zero for a positive duration", () => {
    expect(formatEstimate(0.4)).toBe("about 1 s");
  });
});
