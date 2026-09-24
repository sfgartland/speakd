import { describe, expect, it } from "vitest";
import { parsePageRanges } from "../../src/export/range";

describe("parsePageRanges", () => {
  it("parses a mix of ranges and single pages, 1-based input to 0-based ranges", () => {
    const result = parsePageRanges("12-48, 60");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.ranges).toEqual([
        { startPage: 11, endPage: 47 },
        { startPage: 59, endPage: 59 },
      ]);
    }
  });

  it("merges overlapping and adjacent ranges", () => {
    const result = parsePageRanges("1-5, 6-10, 20-25, 22-30");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.ranges).toEqual([
        { startPage: 0, endPage: 9 },
        { startPage: 19, endPage: 29 },
      ]);
    }
  });

  it("sorts ranges given out of order", () => {
    const result = parsePageRanges("60, 12-48");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.ranges).toEqual([
        { startPage: 11, endPage: 47 },
        { startPage: 59, endPage: 59 },
      ]);
    }
  });

  it("rejects a reversed range", () => {
    const result = parsePageRanges("48-12");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/order|revers/i);
  });

  it("rejects a page number below 1", () => {
    const result = parsePageRanges("0-5");
    expect(result.ok).toBe(false);
  });

  it("rejects unparseable input", () => {
    for (const input of ["", "abc", "12-", "-5", "1,,2", "1..2"]) {
      const result = parsePageRanges(input);
      expect(result.ok, `expected "${input}" to be rejected`).toBe(false);
      if (!result.ok) expect(result.error.length).toBeGreaterThan(0);
    }
  });

  it("tolerates extra whitespace", () => {
    const result = parsePageRanges("  12 - 48 ,   60  ");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.ranges).toEqual([
        { startPage: 11, endPage: 47 },
        { startPage: 59, endPage: 59 },
      ]);
    }
  });
});
