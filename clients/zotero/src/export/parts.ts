// Building the render parts (`{title, text}` per chapter, or a single part
// for an article) that go to the daemon's `render` verb.
//
// Pure: it takes the captured segments, an item title, and a selection
// describing what to include, and answers plain data. Page ranges and
// chapter ranges (export/range.ts, export/outline.ts) are already resolved
// to 0-based page indices by the time they reach here.

import { findReferencesCut, type ReferencesSegment } from "./references";

/** What this module needs of a captured segment. */
export interface PartSegment extends ReferencesSegment {
  readonly pageIndex: number;
}

export interface ChapterSelection {
  readonly title: string;
  readonly startPage: number;
  readonly endPage: number;
}

export interface PageSelection {
  readonly startPage: number;
  readonly endPage: number;
}

export type PartsSelection =
  | { readonly kind: "whole"; readonly skipReferences?: boolean }
  | { readonly kind: "chapters"; readonly chapters: readonly ChapterSelection[] }
  | { readonly kind: "pages"; readonly ranges: readonly PageSelection[] };

export interface Part {
  readonly title: string;
  readonly text: string;
}

/** Book and bookSection get the chapter/page-range flow; everything else is treated as an article. */
export function isBookItemType(itemType: string): boolean {
  return itemType === "book" || itemType === "bookSection";
}

function joinText(segments: readonly PartSegment[]): string {
  return segments
    .map((segment) => segment.text)
    .filter((text) => text.length > 0)
    .join(" ");
}

function segmentsInPages(segments: readonly PartSegment[], startPage: number, endPage: number): PartSegment[] {
  return segments.filter((segment) => segment.pageIndex >= startPage && segment.pageIndex <= endPage);
}

/**
 * Builds the render parts for a selection.
 *
 * A "whole" selection makes one part titled with the item; when
 * `skipReferences` is set, it cuts before the last references heading
 * found in the second half of the document (export/references.ts). A
 * "chapters" selection makes one part per chapter, titled from the
 * outline; a "pages" selection makes one part per range, titled with the
 * item and the pages it covers. Either way, only the pages named in the
 * selection are included, which is what skips a book's front and back
 * matter: those pages simply belong to no chapter or range. A part with no
 * text (an empty or misplaced chapter) is dropped.
 */
export function buildParts(
  segments: readonly PartSegment[],
  itemTitle: string,
  selection: PartsSelection,
): Part[] {
  if (selection.kind === "whole") {
    const cut = selection.skipReferences ? findReferencesCut(segments) : null;
    const included = cut === null ? segments : segments.slice(0, cut);
    return dropEmpty([{ title: itemTitle, text: joinText(included) }]);
  }

  if (selection.kind === "chapters") {
    return dropEmpty(
      selection.chapters.map((chapter) => ({
        title: chapter.title,
        text: joinText(segmentsInPages(segments, chapter.startPage, chapter.endPage)),
      })),
    );
  }

  return dropEmpty(
    selection.ranges.map((range) => ({
      title: `${itemTitle} (p. ${formatPageRange(range)})`,
      text: joinText(segmentsInPages(segments, range.startPage, range.endPage)),
    })),
  );
}

function formatPageRange(range: PageSelection): string {
  const first = range.startPage + 1;
  const last = range.endPage + 1;
  return first === last ? `${first}` : `${first}-${last}`;
}

function dropEmpty(parts: readonly Part[]): Part[] {
  return parts.filter((part) => part.text.length > 0);
}
