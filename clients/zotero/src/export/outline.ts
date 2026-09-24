// Turning a PDF outline into chapter ranges for a book export.
//
// Pure: the outline comes in already resolved to page indices (pdf.js
// destinations are resolved through the reader before this module sees
// them), so this only does arithmetic on page numbers.

/** An outline entry, as pdf.js gives it once its destination is resolved to a page index. */
export interface OutlineItem {
  readonly title: string;
  readonly pageIndex: number;
  readonly items?: readonly OutlineItem[];
}

export interface ChapterRange {
  readonly title: string;
  /** 0-based, inclusive. */
  readonly startPage: number;
  /** 0-based, inclusive. */
  readonly endPage: number;
}

/**
 * The top-level outline entries turned into page ranges: each chapter runs
 * from its own page to the page before the next chapter's, and the last
 * chapter runs to `lastPageIndex` (the document's last page).
 *
 * Nested items (subsections) are flattened into their enclosing top-level
 * chapter rather than becoming chapters of their own -- a book export asks
 * to render by chapter, not by subsection. A book with no outline (or an
 * empty one) answers null, so the caller offers a page range instead of an
 * empty chapter list.
 */
export function outlineToChapters(
  outline: readonly OutlineItem[] | null | undefined,
  lastPageIndex: number,
): ChapterRange[] | null {
  if (outline === null || outline === undefined || outline.length === 0) return null;

  return outline.map((chapter, index) => {
    const next = outline[index + 1];
    const endPage = next === undefined ? lastPageIndex : next.pageIndex - 1;
    return { title: chapter.title, startPage: chapter.pageIndex, endPage };
  });
}
