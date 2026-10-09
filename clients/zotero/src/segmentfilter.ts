// The cross-page running-head filter the plugin lacked: the skill's
// `running_lines`/`strip_pages`
// (clients/claude-code/skills/speakd-audiobook/scripts/prepare_text.py)
// transposed to captured segments (proposal §4.2). It is the backstop for
// whatever Zotero's `flowClass === 'excluded'` misses: a paragraph recurring
// in the same slot on enough pages is a running head, and a body paragraph
// never recurs, page after page, in the same slot. Conservative by
// construction: it only fires on recurrence, never on content.
//
// Pure. The constants are the skill's, mirrored rather than invented
// (prepare_text.py:20-21): a normalised opening or closing paragraph
// recurring on 40% of at least 5 pages.

/** The share of pages a paragraph must recur on to be a running head. */
export const HEADER_SHARE = 0.4;
/** The fewest pages a document must have before anything is a running head. */
export const HEADER_MIN_PAGES = 5;
/** A running head is short; an opening paragraph this long is body text. */
const HEAD_MAX_CHARS = 80;

/** What the filter needs of a segment. */
export interface FilterSegment {
  readonly text: string;
  /**
   * The page the segment starts on. A segment whose page Zotero could not
   * place inherits its predecessor's, exactly as `capturedSegments` places it.
   */
  readonly pageIndex?: number | null;
}

// A page-number-like token: digits plus the OCR confusions of 1/0 (i l I o O),
// with at least one real digit -- so 'i6' and '16' become the same '##' and a
// mangled running head still recurs, while body words are left alone.
const PAGE_TOKEN = /^[0-9OolIil]+$/;

/** Whitespace collapsed, every digit `#`: what makes "Chapter 3" and "Chapter 4" one head. */
function normalise(text: string): string {
  const tokens = text.split(/\s+/).map((token) => {
    const bare = token.replace(/[^0-9A-Za-z]/g, "");
    const pageLike = PAGE_TOKEN.test(bare) && /\d/.test(bare);
    return pageLike ? token.replace(/[0-9OolIil]/g, "#") : token.replace(/\d/g, "#");
  });
  return tokens.join(" ").replace(/\s+/g, " ").trim();
}

/** Whether a paragraph is short enough to be a running head at all. */
const isShort = (text: string): boolean => text.trim().length <= HEAD_MAX_CHARS;

/**
 * The segments that are not running heads, in their order. The result holds
 * the input's own segment objects, so a caller can tell kept from dropped by
 * identity; when nothing is dropped the input itself is returned.
 */
export function filterRunningHeads(segments: readonly FilterSegment[]): readonly FilterSegment[] {
  // Page groups, in order of first appearance, each with the segment indices
  // it holds: a segment without a page lands on its predecessor's.
  const pages = new Map<number, number[]>();
  let page = 0;
  segments.forEach((segment, index) => {
    if (typeof segment.pageIndex === "number") page = segment.pageIndex;
    const group = pages.get(page);
    if (group === undefined) pages.set(page, [index]);
    else group.push(index);
  });
  if (pages.size < HEADER_MIN_PAGES) return segments;

  // The normalised opening or closing paragraph of each page, counted once a
  // page however many times it appears there: `running_lines` over segments.
  const counts = new Map<string, number>();
  for (const indices of pages.values()) {
    const content = indices.filter((index) => segments[index]!.text.trim() !== "");
    if (content.length === 0) continue;
    const first = segments[content[0]!]!;
    const last = segments[content[content.length - 1]!]!;
    const keys = new Set<string>();
    if (isShort(first.text)) keys.add(normalise(first.text));
    if (isShort(last.text)) keys.add(normalise(last.text));
    for (const key of keys) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const running = new Set(
    [...counts.entries()].filter(([, seen]) => seen >= HEADER_SHARE * pages.size).map(([key]) => key),
  );
  if (running.size === 0) return segments;

  // Dropped wherever it appears, as `strip_pages` drops a matching line
  // anywhere on the page: a long body paragraph can never match a short
  // head's normalised text, so only heads and their like go.
  const kept = segments.filter((segment) => !running.has(normalise(segment.text)));
  return kept.length === segments.length ? segments : kept;
}
