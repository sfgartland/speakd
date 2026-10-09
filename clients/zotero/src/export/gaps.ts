// Capture gaps: page text that never became a segment (spec §8.4).
//
// The plugin runs in Zotero's JS, so the diff of a page's raw text against
// its captured segments happens here, not in Python. The raw text comes
// from pdf.js's `getTextContent`, through the same `pdfDocument` reach
// `export/structure.ts readStructure` uses. Everything is best-effort: a
// reader where the reach fails costs the gap report, never a fixture export
// and never a throw into Zotero.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

/** Whitespace collapsed to single spaces, ends trimmed: what the diff keys on. */
function collapse(text: string): string {
  return text.replace(/\s+/gu, " ").trim();
}

/**
 * The raw page's lines that no captured segment's text contains, whitespace
 * collapsed (spec §8.4): the "SDT dropped a body block" class. A blank line
 * is not a gap; a line a captured segment covers is not a gap.
 */
export function pageGaps(rawPageLines: readonly string[], capturedTexts: readonly string[]): string[] {
  const captured = capturedTexts.map(collapse).filter((text) => text !== "");
  const gaps: string[] = [];
  for (const line of rawPageLines) {
    const collapsed = collapse(line);
    if (collapsed === "") continue;
    if (captured.some((text) => text.includes(collapsed))) continue;
    gaps.push(collapsed);
  }
  return gaps;
}

/** One pdf.js text item, as much as the line grouping needs. */
interface TextItem {
  readonly str?: unknown;
  readonly transform?: readonly unknown[];
}

/** Items grouped into lines best-effort by y, then read left-to-right. */
function groupLines(items: readonly TextItem[]): string[] {
  const lines: { y: number; parts: { x: number; str: string }[] }[] = [];
  for (const item of items) {
    if (typeof item.str !== "string") continue;
    const transform = item.transform ?? [];
    const x = typeof transform[4] === "number" ? transform[4] : 0;
    const y = typeof transform[5] === "number" ? transform[5] : 0;
    let line = lines.find((candidate) => Math.abs(candidate.y - y) < 2);
    if (line === undefined) {
      line = { y, parts: [] };
      lines.push(line);
    }
    line.parts.push({ x, str: item.str });
  }
  // Top of the page first (y runs bottom-up), left to right within a line.
  return lines
    .sort((a, b) => b.y - a.y)
    .map((line) =>
      line.parts
        .sort((a, b) => a.x - b.x)
        .map((part) => part.str)
        .join(" ")
        .replace(/\s+/gu, " ")
        .trim(),
    )
    .filter((line) => line !== "");
}

/** The page's raw text as lines, through the `readStructure` pdf.js reach. May throw. */
export async function readRawPageLines(reader: Any, pageIndex: number): Promise<string[]> {
  await reader._initPromise;
  const view = reader._internalReader._primaryView;
  await view.initializedPromise;
  const doc = view._iframeWindow.PDFViewerApplication.pdfDocument;
  const page = await doc.getPage(pageIndex + 1);
  const content = await page.getTextContent();
  return groupLines(Array.isArray(content.items) ? content.items : []);
}

/** The page's gaps, or `{ ok: false }` when the reach or pdf.js fails. Never throws. */
export async function checkPageGaps(
  reader: Any,
  pageIndex: number,
  capturedTexts: readonly string[],
): Promise<{ readonly ok: true; readonly gaps: readonly string[] } | { readonly ok: false }> {
  try {
    return { ok: true, gaps: pageGaps(await readRawPageLines(reader, pageIndex), capturedTexts) };
  } catch {
    return { ok: false };
  }
}
