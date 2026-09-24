// Finding where an article's References/Bibliography section starts, so an
// export can stop before it.
//
// Pure: it only sees the segments Zotero's Read Aloud already produced --
// text and the anchor marking a paragraph's first segment -- and answers in
// segment indices.

/** What this module needs of a segment: its text and paragraph anchor. */
export interface ReferencesSegment {
  readonly text: string;
  readonly anchor?: string | null;
}

/** Zotero marks the first segment of each paragraph with this anchor. */
const PARAGRAPH_START = "paragraphStart";

// A references heading is a short line, not a sentence that happens to
// mention the word. 60 characters comfortably covers "Literaturverzeichnis"
// and any surrounding punctuation, while ruling out a body sentence.
const MAX_HEADING_LENGTH = 60;

const HEADING_PATTERN = /^(references|bibliography|works cited|literatur(verzeichnis)?)$/i;

function isHeading(segment: ReferencesSegment): boolean {
  const text = segment.text.trim();
  if (text.length === 0 || text.length > MAX_HEADING_LENGTH) return false;
  if (segment.anchor !== PARAGRAPH_START) return false;
  return HEADING_PATTERN.test(text);
}

/**
 * The segment index a references/bibliography section starts at, or null
 * when there is none.
 *
 * A table of contents can list "References" near the start of the
 * document, so only a match in the second half counts; the last such match
 * is used, in case an early false positive (an interleaved heading that
 * quotes the word) is followed by the real one.
 */
export function findReferencesCut(segments: readonly ReferencesSegment[]): number | null {
  const half = Math.floor(segments.length / 2);
  for (let index = segments.length - 1; index >= half; index--) {
    const segment = segments[index];
    if (segment !== undefined && isHeading(segment)) return index;
  }
  return null;
}
