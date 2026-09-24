// Sections: the speakd jobs a read is split into.
//
// A read from here to the end of a paper is not one job. It is a run of
// sections, each about a page, enqueued one ahead of the one being spoken.
// A notification can then be heard between two sections, and the cleanup of
// the next section happens while this one speaks rather than before the
// first word. The first section is kept to a couple of segments, so the
// first sound comes as soon as speakd can make it.

/** What a section needs of a Zotero Read Aloud segment: its text alone. */
export interface SegmentText {
  readonly text: string;
}

export interface Section {
  /** The Zotero segments this section reads, by their index in the document. */
  readonly segmentIndices: readonly number[];
  /** Their texts, joined with single spaces: what is cleaned and enqueued. */
  readonly text: string;
  /** `offsets[k]` is where segment `segmentIndices[k]` starts in `text`. */
  readonly offsets: readonly number[];
}

// About a page of a paper. Long enough that a section boundary is rare,
// short enough that cleaning the next one never holds up the voice.
const DEFAULT_SIZE = 1800;

/**
 * The sections reading `segments` from `start` to the end.
 *
 * The first holds at most `firstSize` segments; each later one as many as
 * fit in `size` characters. A segment longer than `size` is a section of its
 * own rather than being split, since a section boundary inside a sentence
 * would be heard as a pause in the middle of it. Segments with no text are
 * skipped: there is nothing to say for them, and nothing to highlight.
 */
export function buildSections(
  segments: readonly SegmentText[],
  start: number,
  firstSize = 2,
  size = DEFAULT_SIZE,
): Section[] {
  const sections: Section[] = [];
  let indices: number[] = [];
  let offsets: number[] = [];
  let text = "";

  const close = (): void => {
    if (indices.length === 0) return;
    sections.push({ segmentIndices: indices, text, offsets });
    indices = [];
    offsets = [];
    text = "";
  };

  for (let index = Math.max(0, start); index < segments.length; index++) {
    const piece = segments[index]!.text.trim();
    if (piece === "") continue;
    const limit = sections.length === 0 ? firstSize : Infinity;
    const joined = text === "" ? piece.length : text.length + 1 + piece.length;
    if (indices.length >= limit || (indices.length > 0 && joined > size)) close();
    offsets.push(text === "" ? 0 : text.length + 1);
    text = text === "" ? piece : `${text} ${piece}`;
    indices.push(index);
  }
  close();
  return sections;
}
