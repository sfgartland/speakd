import { describe, expect, it } from "vitest";
import { fixtureContent } from "../../src/export/fixture";
import { locatorFor, serialiseLabelFile, type LabelFile } from "../../src/labelstore";
import type { CapturedSegment } from "../../src/session";

function segment(
  text: string,
  pageIndex = 0,
  anchor: string | null = "paragraphStart",
  firstRectY: number | null = 740,
): CapturedSegment {
  return { text, pageIndex, anchor, rectCount: firstRectY === null ? null : 1, firstRectY };
}

function labelFile(labels: LabelFile["labels"]): LabelFile {
  return {
    schema: 1,
    attachmentKey: "ATTACH01",
    documentSha256: "document-hash",
    capture: { zoteroVersion: "10.0.6", segments: 3, labelledAt: 0 },
    labels,
  };
}

describe("fixtureContent", () => {
  const segments = [
    segment("Body one.", 0, "paragraphStart", 770),
    segment("Junk to drop.", 0, "paragraphStart", 760),
    segment("Body two.", 0, "paragraphStart", 740),
  ];
  const file = labelFile([
    { locator: locatorFor(segments[1]!), category: "junk", labelledAt: 0, note: "" },
  ]);
  const overrides = new Map<number, "junk">([[1, "junk"]]);

  it("writes the four fixture files", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, pdfPath: "/owner/Köhler.pdf", gaps: null, command: "handles()[0].exportFixture(...)" });
    expect(Object.keys(out).sort()).toEqual(["README.md", "capture.jsonl", "expected.json", "labels.json"]);
  });

  it("writes labels.json verbatim, the ground truth", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, gaps: null });
    expect(out["labels.json"]).toBe(serialiseLabelFile(file));
  });

  it("writes capture.jsonl in the corpus.test shape", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, gaps: null });
    const parsed = out["capture.jsonl"]
      .split("\n")
      .filter((line) => line.trim() !== "")
      .map((line) => JSON.parse(line) as Record<string, unknown>)
      .filter((entry) => typeof entry.text === "string");
    expect(parsed).toHaveLength(3);
    expect(parsed[0]).toMatchObject({ index: 0, text: "Body one.", pageIndex: 0, anchor: "paragraphStart", firstRectY: 770 });
  });

  it("snapshots the expected pipeline outcome with the overrides applied", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, gaps: null });
    const expected = JSON.parse(out["expected.json"]) as { tags: string[]; parts: { text: string }[] };
    expect(expected.tags).toEqual(["body", "drop", "body"]);
    const spoken = expected.parts.map((part) => part.text).join(" ");
    expect(spoken).toContain("Body one.");
    expect(spoken).not.toContain("Junk to drop.");
  });

  it("records the source sha, the bound and the gap report in the README", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, pdfPath: "/owner/Köhler.pdf", gaps: null });
    expect(out["README.md"]).toContain("document-hash");
    expect(out["README.md"]).toContain("/owner/Köhler.pdf");
    expect(out["README.md"]).toContain("Pages: 1");
    expect(out["README.md"]).toContain("bound");
    expect(out["README.md"]).toContain("gap check unavailable");
  });

  it("names a planted dropped block in the README", () => {
    const out = fixtureContent({ segments, labels: file, policy: "classify", overrides, gaps: ["A block SDT dropped."] });
    expect(out["README.md"]).toContain("A block SDT dropped.");
  });
});
