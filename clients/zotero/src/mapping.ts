// Which Zotero segment to highlight while each of speakd's sentences plays.
//
// speakd splits what it is given into sentences of its own, and they need
// not line up with Zotero's: one of its sentences can span two of Zotero's
// segments, or one segment can be several of its sentences. `started` lists
// every sentence with its span, and `position` names the one playing by its
// index, so the whole plan is made once, at `started`, and a position is a
// lookup -- never arithmetic on a span at the moment it is heard.

import { lookup, type OffsetMap } from "./offsetmap";
import type { Section } from "./sections";

/** One of speakd's sentences, as `started.segments` lists it. */
export interface DaemonSegment {
  readonly index: number;
  readonly span_start: number;
  readonly span_end: number;
  /** What speakd will say for it, when `started` names it -- the defence's evidence. */
  readonly text?: string;
}

// How many letters of a sentence's opening the defence looks for. Enough that
// a coincidence is unlikely, few enough that cleanup trimming a sentence's
// tail, or a sentence running on into the next segment, still passes.
const EVIDENCE_LETTERS = 12;

/** Letters and digits only, lower-cased: what survives cleanup and normalisation alike. */
function normalise(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
}

/**
 * For each daemon sentence, the index of the Zotero segment to highlight
 * while it plays: the one its first spoken character is in. A sentence that
 * spans two segments therefore highlights the first, never neither.
 *
 * `map` carries an index into the text speakd was given (after cleanup)
 * back to `section.text`. `textStart` is where that cleaned text begins in
 * what speakd spoke: speakd may put a channel's label before it, and the
 * spans count from the label. A sentence lying wholly outside the section's
 * text is left out: better no highlight than a guessed one.
 */
export function planHighlights(
  section: Section,
  daemonSegments: readonly DaemonSegment[],
  map: OffsetMap,
  textStart = 0,
): Map<number, number> {
  const plan = new Map<number, number>();
  const cleanedLength = map.length - 1;
  for (const segment of daemonSegments) {
    const start = Math.max(segment.span_start, textStart) - textStart;
    const end = segment.span_end - textStart;
    if (start >= end || start >= cleanedLength || end <= 0) continue;

    let from = lookup(map, start);
    const until = lookup(map, end);
    // A span that begins on whitespace -- the space joining two segments --
    // is about what follows it, not the segment that space closed.
    while (from < until && /\s/.test(section.text[from] ?? "")) from++;

    const k = segmentAt(section.offsets, from);
    const zoteroIndex = section.segmentIndices[k];
    if (zoteroIndex === undefined) continue;
    // The mis-mapping defence: the sentence's opening words must be in the
    // segment the offsets chose. A map that went wrong anywhere upstream
    // would otherwise light a confident, wrong sentence -- the one failure
    // worse than lighting none.
    if (segment.text !== undefined) {
      // Past any label speakd said first, which is no part of the document.
      const spoken = segment.text.slice(Math.max(0, textStart - segment.span_start));
      const chosenEnd = section.offsets[k + 1] ?? section.text.length;
      const chosen = section.text.slice(section.offsets[k], chosenEnd);
      // Only as much of the opening as the chosen segment still holds from
      // where the sentence starts in it: a sentence that runs on past a
      // short heading has the rest of its opening in the next segment.
      const room = normalise(section.text.slice(from, chosenEnd)).length;
      const head = normalise(spoken).slice(0, Math.min(EVIDENCE_LETTERS, room));
      if (head && !normalise(chosen).includes(head)) continue;
    }
    plan.set(segment.index, zoteroIndex);
  }
  return plan;
}

/** The position in `offsets` of the last segment starting at or before `at`. */
function segmentAt(offsets: readonly number[], at: number): number {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (offsets[middle]! <= at) low = middle;
    else high = middle - 1;
  }
  return low;
}
