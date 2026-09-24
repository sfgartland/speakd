// Parsing a page-range field like "12-48, 60" for a book export.
//
// Pure text parsing: the user's 1-based page numbers, as printed in a PDF
// viewer, become 0-based page-index ranges for the rest of the export
// pipeline.

export interface PageRange {
  /** 0-based, inclusive. */
  readonly startPage: number;
  /** 0-based, inclusive. */
  readonly endPage: number;
}

export type ParsePageRangesResult =
  | { readonly ok: true; readonly ranges: readonly PageRange[] }
  | { readonly ok: false; readonly error: string };

function fail(error: string): ParsePageRangesResult {
  return { ok: false, error };
}

// A term is either "N" or "N-M", with optional surrounding whitespace.
const TERM_PATTERN = /^(\d+)(?:-(\d+))?$/;

/**
 * Parses a comma-separated list of pages and page ranges, such as
 * "12-48, 60", into 0-based ranges merged where they touch or overlap.
 *
 * Page numbers are 1-based, as a reader shows them; the result subtracts
 * one so callers can index pages directly. Anything unparseable, a page
 * below 1, or a range whose end comes before its start is an error with a
 * message meant for the dialog, not a thrown exception -- a typo while
 * typing a page range is an expected, correctable input.
 */
export function parsePageRanges(input: string): ParsePageRangesResult {
  const terms = input.split(",").map((term) => term.trim());
  if (terms.length === 0 || terms.some((term) => term === "")) {
    return fail("Enter a page or range, such as \"12-48, 60\".");
  }

  const ranges: PageRange[] = [];
  for (const term of terms) {
    const match = TERM_PATTERN.exec(term.replace(/\s*-\s*/, "-"));
    if (match === null) return fail(`"${term}" is not a page or a range like "12-48".`);
    const first = Number(match[1]);
    const second = match[2] === undefined ? first : Number(match[2]);
    if (first < 1 || second < 1) return fail("Page numbers start at 1.");
    if (second < first) return fail(`"${term}" is reversed -- the range's end comes before its start.`);
    ranges.push({ startPage: first - 1, endPage: second - 1 });
  }

  return { ok: true, ranges: mergeRanges(ranges) };
}

function mergeRanges(ranges: readonly PageRange[]): PageRange[] {
  const sorted = [...ranges].sort((a, b) => a.startPage - b.startPage);
  const merged: PageRange[] = [];
  for (const range of sorted) {
    const last = merged[merged.length - 1];
    if (last !== undefined && range.startPage <= last.endPage + 1) {
      merged[merged.length - 1] = { startPage: last.startPage, endPage: Math.max(last.endPage, range.endPage) };
    } else {
      merged.push(range);
    }
  }
  return merged;
}
