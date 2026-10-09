// Building the render parts (`{title, text}` per chapter, or a single part
// for an article) that go to the daemon's `render` verb.
//
// Pure: it takes the captured segments, an item title, and a selection
// describing what to include, and answers plain data. Page ranges and
// chapter ranges (export/range.ts, export/outline.ts) are already resolved
// to 0-based page indices by the time they reach here.

import { cleanup } from "../cleanup";
import { tagSegments, type FootnotePolicy } from "../footnotes";
import type { LabelCategory } from "../labelstore";
import type { FootnoteKind } from "../llm";
import type { TaggedSegment } from "../sections";
import { findReferencesCut, type ReferencesSegment } from "./references";

/** What this module needs of a captured segment. */
export interface PartSegment extends ReferencesSegment {
  readonly pageIndex: number;
}

/** A captured segment as the footnote plan leaves it: tagged for what the part reads. */
type PlannedSegment = PartSegment & Pick<TaggedSegment, "tag"> & { readonly footnoteKind?: FootnoteKind };

/** What the parts' filters take: the footnote policy (the `footnote_policy` pref). */
export interface PartsOptions {
  readonly footnotes?: FootnotePolicy;
  /** The Stage B batch's labels, by original captured index (Task 11). */
  readonly footnoteKinds?: ReadonlyMap<number, FootnoteKind>;
  /** The labeler's whole-segment overrides, by original captured index (spec §8.2). */
  readonly overrides?: ReadonlyMap<number, LabelCategory>;
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

/**
 * A part's text: its body segments in order, the kept footnotes deferred to
 * the end (never read inline mid-flow), and the joined text through the
 * cleanup rules -- an export has no offset map to protect, so the rules run
 * as freely as the read's do.
 */
function joinText(segments: readonly PlannedSegment[]): string {
  // A `classify` footnote the batch labelled a citation goes; anything else
  // `classify` (including a miss) is kept, deferred to the part's end.
  const gone = (segment: PlannedSegment): boolean =>
    segment.tag === "drop" || (segment.tag === "classify" && segment.footnoteKind === "citation");
  const body = segments.filter(
    (segment) => !gone(segment) && segment.tag !== "defer" && segment.tag !== "classify",
  );
  const deferred = segments.filter(
    (segment) => !gone(segment) && (segment.tag === "defer" || segment.tag === "classify"),
  );
  const raw = [...body, ...deferred]
    .map((segment) => segment.text)
    .filter((text) => text.length > 0)
    .join(" ");
  return cleanup(raw).text;
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
 *
 * Before any selection, the segments run through the read's filters
 * (footnotes.ts `tagSegments`): running heads and dropped footnotes never
 * appear, kept footnotes wait for the end of the part that owns their page,
 * and each part's joined text goes through the cleanup rules. The old raw
 * join is the no-op case: nothing filtered, nothing to clean.
 */
export function buildParts(
  segments: readonly PartSegment[],
  itemTitle: string,
  selection: PartsSelection,
  options: PartsOptions = {},
): Part[] {
  // The same filters as the read (proposal §4.2), before any selection: a
  // running head recurs no matter which pages are chosen, and a dropped
  // footnote never appears in a range either. The Stage B kinds, keyed by
  // original captured index, ride along on their segments.
  const withKinds: PlannedSegment[] = segments.map((segment, index) => ({
    ...segment,
    footnoteKind: options.footnoteKinds?.get(index),
  }));
  const { tagged: planned } = tagSegments(withKinds, options.footnotes ?? "classify", options.overrides);
  if (selection.kind === "whole") {
    const cut = selection.skipReferences ? findReferencesCut(planned) : null;
    const included = cut === null ? planned : planned.slice(0, cut);
    return dropEmpty([{ title: itemTitle, text: joinText(included) }]);
  }

  if (selection.kind === "chapters") {
    return dropEmpty(
      selection.chapters.map((chapter) => ({
        title: chapter.title,
        text: joinText(segmentsInPages(planned, chapter.startPage, chapter.endPage)),
      })),
    );
  }

  return dropEmpty(
    selection.ranges.map((range) => ({
      title: `${itemTitle} (p. ${formatPageRange(range)})`,
      text: joinText(segmentsInPages(planned, range.startPage, range.endPage)),
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
