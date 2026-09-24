// Where a skip or a selection leads, in Zotero's own segments.
//
// Pure: it sees segments as texts and paragraph anchors, which is all Zotero
// gives them that matters here, and answers in segment indices.

/** Zotero marks the first segment of each paragraph with this anchor. */
const PARAGRAPH_START = "paragraphStart";

function isParagraphStart(anchors: readonly (string | null | undefined)[], index: number): boolean {
  return index === 0 || anchors[index] === PARAGRAPH_START;
}

/**
 * The segment a paragraph skip from `pos` lands on: ahead, the next
 * paragraph's first segment (null when there is none); back, the first
 * segment of the paragraph being read, or of the one before it when `pos` is
 * already a paragraph's first -- the way a player's "previous" works.
 */
export function paragraphTarget(
  anchors: readonly (string | null | undefined)[],
  pos: number,
  direction: 1 | -1,
): number | null {
  if (direction === 1) {
    for (let index = pos + 1; index < anchors.length; index++) {
      if (isParagraphStart(anchors, index)) return index;
    }
    return null;
  }
  let start = pos;
  while (start > 0 && !isParagraphStart(anchors, start)) start--;
  if (start < pos || start === 0) return start;
  start--;
  while (start > 0 && !isParagraphStart(anchors, start)) start--;
  return start;
}

// Enough of the selection's last characters to find it by, and few enough
// that a short selection still has them.
const PROBE = 16;

function squash(text: string): string {
  return text.replace(/\s+/g, "");
}

/**
 * The last segment a "Read selection" that starts at segment `start`
 * should read: the one holding the selection's last characters.
 *
 * The selected text and Zotero's segments differ -- the selection keeps line
 * breaks, and citations Zotero elides from its segments -- so whitespace is
 * ignored, and when the selection's end cannot be found (it was elided),
 * the selection's length is counted out over the segments instead. Either
 * way the answer lies between `start` and the last segment.
 */
export function selectionEnd(segments: readonly { text: string }[], start: number, selected: string): number {
  const last = segments.length - 1;
  const wanted = squash(selected);
  if (wanted === "" || start >= last) return Math.min(start, last);

  const texts = segments.slice(start).map((segment) => squash(segment.text));
  // Where the selection begins in its first segment, when it can be told.
  const head = texts[0]!.indexOf(wanted.slice(0, PROBE));
  const begin = head < 0 ? 0 : head;

  const joined = texts.join("");
  const bounds: number[] = [];
  let end = 0;
  for (const text of texts) bounds.push((end += text.length));
  const segmentAt = (offset: number): number => {
    const found = bounds.findIndex((bound) => offset < bound);
    return start + (found < 0 ? texts.length - 1 : found);
  };

  const tail = wanted.slice(-PROBE);
  const at = joined.indexOf(tail, begin);
  if (at >= 0) return segmentAt(at + tail.length - 1);
  return segmentAt(begin + wanted.length - 1);
}
