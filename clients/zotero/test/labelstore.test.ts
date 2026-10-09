import { describe, expect, it } from "vitest";
import {
  locatorFor,
  matchLabels,
  parseLabelFile,
  serialiseLabelFile,
  type LabelFile,
  type LabelRecord,
} from "../src/labelstore";
import type { CapturedSegment } from "../src/session";

function segment(
  text: string,
  pageIndex = 0,
  anchor: string | null = "paragraphStart",
  firstRectY: number | null = 100,
): CapturedSegment {
  return { text, pageIndex, anchor, rectCount: firstRectY === null ? null : 1, firstRectY };
}

function record(locator: LabelRecord["locator"], category: LabelRecord["category"]): LabelRecord {
  return { locator, category, labelledAt: 1760000000, note: "" };
}

function file(labels: readonly LabelRecord[]): LabelFile {
  return {
    schema: 1,
    attachmentKey: "ATTACH01",
    documentSha256: "document-hash",
    capture: { zoteroVersion: "10.0.6", segments: 812, labelledAt: 1760000000 },
    labels: [...labels],
  };
}

describe("locatorFor", () => {
  it("carries the segment's place and a hash of its full collapsed text", () => {
    // sha256("abc") is the classic vector: the hash is real, not a placeholder.
    const locator = locatorFor(segment("abc", 34, "paragraphStart", 101.4));
    expect(locator).toEqual({
      page: 34,
      rectY: 101.4,
      anchor: "paragraphStart",
      prefix: "abc",
      textSha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    });
  });

  it("collapses whitespace before hashing, and takes the first 48 characters as the prefix", () => {
    const long = `  The   opening clause is identical across the split, and the tail differs.\n`;
    const locator = locatorFor(segment(long));
    expect(locator.prefix).toBe("The opening clause is identical across the split");
    expect(locator.prefix).toHaveLength(48);
  });

  it("keeps a null rect y and anchor", () => {
    const locator = locatorFor(segment("text", 1, null, null));
    expect(locator.rectY).toBeNull();
    expect(locator.anchor).toBeNull();
  });
});

describe("matchLabels", () => {
  it("matches the same page and full text hash exactly", () => {
    const target = segment("A footnote in the bottom band.", 5);
    const label = record(locatorFor(target), "footnote");
    const { overrides, unmatched } = matchLabels(file([label]), [target]);
    expect([...overrides.entries()]).toEqual([[0, "footnote"]]);
    expect(unmatched).toEqual([]);
  });

  it("matches on prefix and anchor when a re-segmentation moved the boundaries around the text", () => {
    const original = "The opening clause is identical across the split, and this tail differs.";
    const resegmented = segment("The opening clause is identical across the split, and the tail changed.", 3);
    const label = record(locatorFor(segment(original, 3)), "running-head");
    const { overrides, unmatched } = matchLabels(file([label]), [resegmented]);
    expect([...overrides.entries()]).toEqual([[0, "running-head"]]);
    expect(unmatched).toEqual([]);
  });

  it("leaves a near-miss prefix unmatched, and inert", () => {
    const original = "The opening clause is identical across the split, and this tail differs.";
    const different = segment("The opening clause is different across the split, and this tail differs.", 3);
    const label = record(locatorFor(segment(original, 3)), "junk");
    const { overrides, unmatched } = matchLabels(file([label]), [different]);
    expect([...overrides.entries()]).toEqual([]);
    expect(unmatched).toHaveLength(1);
  });

  it("leaves a label whose page drifted unmatched", () => {
    const target = segment("The same sentence, on a page the capture moved it off.", 4);
    const label = record(locatorFor(segment(target.text, 3)), "page-number");
    const { overrides, unmatched } = matchLabels(file([label]), [target]);
    expect(overrides.size).toBe(0);
    expect(unmatched).toHaveLength(1);
  });

  it("never matches one segment twice", () => {
    const target = segment("Only one segment here.", 0);
    const first = record(locatorFor(target), "body");
    const second = record(locatorFor(target), "junk");
    const { overrides, unmatched } = matchLabels(file([first, second]), [target]);
    expect([...overrides.entries()]).toEqual([[0, "body"]]);
    expect(unmatched).toEqual([second]);
  });
});

describe("parseLabelFile", () => {
  it("round-trips the spec §7.1 example", () => {
    const example = file([
      record(
        {
          page: 34,
          rectY: 101.4,
          anchor: "paragraphStart",
          prefix: "il found recently that these words",
          textSha256: "0a6c1f",
        },
        "footnote",
      ),
    ]);
    const parsed = parseLabelFile(serialiseLabelFile(example));
    expect(parsed).toEqual({ ok: true, file: example });
  });

  it("refuses unparsable JSON", () => {
    const parsed = parseLabelFile("{not json");
    expect(parsed.ok).toBe(false);
  });

  it("refuses an unknown schema", () => {
    const parsed = parseLabelFile(JSON.stringify({ ...file([]), schema: 2 }));
    expect(parsed).toEqual({ ok: false, why: "unknown label schema: 2" });
  });

  it("refuses a value with no labels list", () => {
    const parsed = parseLabelFile(JSON.stringify({ ...file([]), labels: undefined }));
    expect(parsed.ok).toBe(false);
  });

  it("refuses a schema-1 file with a malformed label record", () => {
    const parsed = parseLabelFile(JSON.stringify({ ...file([]), labels: [{ category: "body" }] }));
    expect(parsed).toEqual({ ok: false, why: "the label file has a malformed label record" });
  });
});
