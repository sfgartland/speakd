import { describe, expect, it } from "vitest";
import { findReferencesCut } from "../../src/export/references";

// Segments as `export/references.ts` sees them: text, and the anchor that
// marks a paragraph's first segment, which a heading always is.
function heading(text: string) {
  return { text, anchor: "paragraphStart" };
}
function body(text: string) {
  return { text, anchor: null };
}

describe("findReferencesCut", () => {
  it("returns null when nothing matches", () => {
    const segments = [heading("Introduction"), body("Some text."), heading("Conclusion"), body("More text.")];
    expect(findReferencesCut(segments)).toBeNull();
  });

  it("finds a References heading in the second half", () => {
    const segments = [
      heading("Introduction"),
      body("Some text."),
      body("More filler to push the heading past the midpoint."),
      heading("References"),
      body("Smith, J. (2020). A paper."),
    ];
    expect(findReferencesCut(segments)).toBe(3);
  });

  it("ignores a matching heading in the first half, such as a table of contents entry", () => {
    const segments = [
      heading("References"), // a TOC entry, early in the document
      body("Introduction text."),
      body("More body text."),
      body("Even more body text to fill out the second half."),
      body("And more still, so the TOC entry stays in the first half."),
    ];
    expect(findReferencesCut(segments)).toBeNull();
  });

  it("picks the LAST matching heading in the second half, not the first", () => {
    const segments = [
      body("filler 1"),
      body("filler 2"),
      heading("Bibliography"),
      body("An interleaved section that isn't actually the reference list."),
      heading("Works Cited"),
      body("Smith, J. (2020)."),
    ];
    expect(findReferencesCut(segments)).toBe(4);
  });

  it("matches case-insensitively, including the German forms", () => {
    for (const title of ["references", "BIBLIOGRAPHY", "Works cited", "literatur", "Literaturverzeichnis"]) {
      const segments = [body("filler"), body("filler"), body("filler"), heading(title), body("entry")];
      expect(findReferencesCut(segments)).toBe(3);
    }
  });

  it("requires the heading to start a paragraph", () => {
    const segments = [body("filler"), body("filler"), body("filler"), body("References"), body("entry")];
    expect(findReferencesCut(segments)).toBeNull();
  });

  it("requires the heading to be short, not a sentence that happens to contain the word", () => {
    const longHeading = heading(
      "References to prior work are discussed throughout this section at some length",
    );
    const segments = [body("filler"), body("filler"), body("filler"), longHeading, body("entry")];
    expect(findReferencesCut(segments)).toBeNull();
  });

  it("treats a heading with only trailing whitespace as still matching", () => {
    const segments = [body("filler"), body("filler"), body("filler"), heading("References \n"), body("entry")];
    expect(findReferencesCut(segments)).toBe(3);
  });
});
