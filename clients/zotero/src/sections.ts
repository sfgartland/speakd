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

/**
 * A segment as the footnote plan leaves it: `body` (or untagged) reads in
 * place, `drop` never appears, `defer` waits for the end of the section that
 * owns its page. Reordering whole segments is map-honest -- highlights
 * resolve by segment index -- so deferred footnotes keep their true
 * original indices and highlight their own rects.
 */
export interface TaggedSegment extends SegmentText {
  readonly tag?: "body" | "drop" | "defer" | "classify";
  /** The page it starts on; a segment without one inherits its predecessor's. */
  readonly pageIndex?: number | null;
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
 *
 * A segment the footnote plan tagged is read by its tag: a dropped one never
 * appears, a deferred or `classify` one is held back and lands at the end of
 * the section that owns its page -- the one holding the last body segment of
 * that page or an earlier one. `classify` is held back exactly as `defer` is
 * (a wrong kept footnote beats a wrong dropped paragraph); a Stage B
 * classifier may later resolve it to a drop. Sizes and the first-section
 * rule are unchanged; a footnote tail rides along. Original indices are kept
 * throughout, so a deferred footnote highlights its own rect. With `inline`
 * the kept ones are not held back at all: they read where they sit.
 */
export function buildSections(
  segments: readonly TaggedSegment[],
  start: number,
  firstSize = 2,
  size = DEFAULT_SIZE,
  inline = false,
): Section[] {
  // The page of each segment, a pageless one inheriting its predecessor's:
  // the page a deferred footnote belongs to.
  const pages: number[] = [];
  let page = 0;
  for (const segment of segments) {
    if (typeof segment.pageIndex === "number") page = segment.pageIndex;
    pages.push(page);
  }

  const body: number[] = [];
  const waiting: number[] = [];
  for (let index = Math.max(0, start); index < segments.length; index++) {
    const segment = segments[index]!;
    if (segment.text.trim() === "") continue;
    if (segment.tag === "drop") continue;
    if (!inline && (segment.tag === "defer" || segment.tag === "classify")) waiting.push(index);
    else body.push(index);
  }
  // A document with nothing but deferred footnotes still reads them, in order.
  if (body.length === 0) body.push(...waiting.splice(0));

  // Sections over the body, exactly as ever: the first at most `firstSize`
  // segments, each later one as many as fit in `size` characters.
  const pieces: number[][] = [];
  let indices: number[] = [];
  let text = "";

  const close = (): void => {
    if (indices.length === 0) return;
    pieces.push(indices);
    indices = [];
    text = "";
  };

  for (const index of body) {
    const piece = segments[index]!.text.trim();
    const limit = pieces.length === 0 ? firstSize : Infinity;
    const joined = text === "" ? piece.length : text.length + 1 + piece.length;
    if (indices.length >= limit || (indices.length > 0 && joined > size)) close();
    text = text === "" ? piece : `${text} ${piece}`;
    indices.push(index);
  }
  close();

  // Which section each body position is in, and where each deferred segment
  // lands: with its page's last body segment, or an earlier page's -- the
  // section that owns its page. A footnote of a page with no body left rides
  // with the first section that has any.
  const owners: number[] = [];
  pieces.forEach((section, sectionIndex) => {
    for (const _ of section) owners.push(sectionIndex);
  });
  const tails: number[][] = pieces.map(() => []);
  let at = 0;
  for (const index of waiting) {
    while (at < body.length && pages[body[at]!]! <= pages[index]!) at++;
    tails[at > 0 ? owners[at - 1]! : 0]!.push(index);
  }

  const sections: Section[] = [];
  pieces.forEach((sectionIndices, sectionIndex) => {
    const all = [...sectionIndices, ...tails[sectionIndex]!];
    let text = "";
    const offsets: number[] = [];
    for (const index of all) {
      const piece = segments[index]!.text.trim();
      offsets.push(text === "" ? 0 : text.length + 1);
      text = text === "" ? piece : `${text} ${piece}`;
    }
    sections.push({ segmentIndices: all, text, offsets });
  });
  return sections;
}
