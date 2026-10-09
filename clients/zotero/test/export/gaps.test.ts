import { describe, expect, it } from "vitest";
import { checkPageGaps, pageGaps, readRawPageLines } from "../../src/export/gaps";

describe("pageGaps", () => {
  it("reports a planted dropped block and nothing else", () => {
    const gaps = pageGaps(
      ["The body paragraph opens the page.", "A block SDT dropped before the plugin saw it.", "More body follows."],
      ["The body paragraph opens the page. More body follows."],
    );
    expect(gaps).toEqual(["A block SDT dropped before the plugin saw it."]);
  });

  it("reports nothing for an empty page", () => {
    expect(pageGaps([], ["Anything at all."])).toEqual([]);
  });

  it("reports nothing when the capture matches the page exactly", () => {
    expect(pageGaps(["A single line of the page."], ["A single line of the page."])).toEqual([]);
  });

  it("collapses whitespace before comparing, and tolerates a capture that spans raw lines", () => {
    expect(pageGaps(["First  part", "second\tpart."], ["First part second part."])).toEqual([]);
  });

  it("ignores a blank raw line", () => {
    expect(pageGaps(["", "   ", "Real line."], ["Real line."])).toEqual([]);
  });
});

describe("readRawPageLines", () => {
  it("reads a page's text items through the pdf.js reach and groups them into lines", async () => {
    const lines = await readRawPageLines(fakeReader(), 2);
    expect(lines).toEqual(["Top line", "Bottom line"]);
  });

  it("fails when the reader has no pdf.js reach", async () => {
    await expect(readRawPageLines({}, 0)).rejects.toBeDefined();
  });
});

describe("checkPageGaps", () => {
  it("is unavailable, not a throw, when the reach fails", async () => {
    expect(await checkPageGaps({}, 0, ["anything"])).toEqual({ ok: false });
  });

  it("reports the page's gaps when the reach works", async () => {
    expect(await checkPageGaps(fakeReader(), 2, ["Top line"])).toEqual({ ok: true, gaps: ["Bottom line"] });
  });

  it("is unavailable when getTextContent rejects", async () => {
    const reader = fakeReader();
    reader._internalReader._primaryView._iframeWindow.PDFViewerApplication.pdfDocument.getPage = async () => ({
      getTextContent: async () => {
        throw new Error("no text layer");
      },
    });
    expect(await checkPageGaps(reader, 0, [])).toEqual({ ok: false });
  });
});

/* eslint-disable @typescript-eslint/no-explicit-any */
function fakeReader(): any {
  const items = [
    { str: "Bottom", transform: [1, 0, 0, 1, 40, 700] },
    { str: "line", transform: [1, 0, 0, 1, 80, 701] },
    { str: "Top", transform: [1, 0, 0, 1, 40, 760] },
    { str: "line", transform: [1, 0, 0, 1, 80, 760] },
  ];
  return {
    _initPromise: Promise.resolve(),
    _internalReader: {
      _primaryView: {
        initializedPromise: Promise.resolve(),
        _iframeWindow: {
          PDFViewerApplication: {
            pdfDocument: {
              getPage: async () => ({ getTextContent: async () => ({ items }) }),
            },
          },
        },
      },
    },
  };
}
