// Footnote detection and the deterministic classification under a policy
// (proposal §4.2/§5.2): which captured segments are footnotes, and of those,
// which the policy drops, defers to their section's end, or leaves to a
// later semantic stage. Detection is structural -- a paragraph that opens
// with a footnote marker and starts in the bottom band of its page -- never
// content-based; classification is shape-based, and everything it cannot
// decide is tagged `classify`, the default of which is *kept* (deferred),
// never dropped: a wrong kept footnote is preferable to a wrong dropped
// paragraph.
//
// Pure. Geometry is Stage 0's measured convention
// (docs/design/2026-10-08-pdf-text-cleanup-stage0.md (c)): rect y runs
// bottom-up, so the bottom band of a page is *low* y, the lowest share of
// the page's observed y-range.

/** How a read's footnotes are treated (proposal §3, the brief's open question 1). */
export type FootnotePolicy = "classify" | "drop_all" | "defer_all";

/** What the plan says of one segment. */
export type FootnoteTag = "body" | "drop" | "defer" | "classify";

/** Per segment index: what the read does with it, and which were dropped. */
export interface FootnotePlan {
  /** `tags[i]` is segment `i`'s plan; body until detection says otherwise. */
  readonly tags: readonly FootnoteTag[];
  /** Every segment the plan drops: the filter's audit trail, for the log. */
  readonly drops: readonly number[];
}

export interface FootnoteOptions {
  /**
   * The bottom band as a share of the page's observed y-range. The
   * provisional 25% comes from the plan; Stage 0's dumps hold it on the
   * fixtures (footnotes at y <= 140, body at >= 641 of an 842-pt page), but
   * its final value is conditional on the owner's failing sample.
   */
  readonly bottomBand?: number;
}

/** What detection needs of a segment. */
export interface PlanSegment {
  readonly text: string;
  readonly anchor: string | null;
  /** The page it starts on; null inherits its predecessor's. */
  readonly pageIndex?: number | null;
  /** The y of its first rect, or null when it carries none. */
  readonly firstRectY?: number | null;
}

/** The provisional bottom band (Stage 0 (c)); see `FootnoteOptions.bottomBand`. */
export const BOTTOM_BAND = 0.25;

// A short footnote is almost always a bare citation; real prose runs long.
// The in-between -- the `classify` residue -- is deliberately wide: what a
// rule cannot decide is not decided by a rule.
const SHORT_FOOTNOTE = 48;
const LONG_FOOTNOTE = 140;

/** Every footnote marker the taxonomy names: superscripts, digits, n-digits, symbols. */
const MARKER = /^(?:[¹²³⁰-⁹]{1,3}|\d{1,2}[.:\s]|n\d{1,2}|\*+|[†‡¶])/u;
const SUPERSCRIPT = "⁰¹²³⁴⁵⁶⁷⁸⁹";

/** The number a marker opens with, for sequential runs; null when it has none. */
function markerNumber(text: string): number | null {
  const head = text.slice(0, 3);
  if (/^[¹²³⁰-⁹]{1,3}/u.test(head)) {
    let value = 0;
    for (const character of head) {
      const digit = SUPERSCRIPT.indexOf(character);
      if (digit < 0) break;
      value = value * 10 + digit;
    }
    return value;
  }
  const plain = /^(?:\d{1,2}|n(\d{1,2}))/.exec(text);
  if (plain === null) return null;
  return Number(plain[1] ?? plain[0]);
}

/** Whether the marker opens a segment's text. */
const hasMarker = (text: string): boolean => MARKER.test(text.trimStart());

/** A citation's shape, when the text holds one of its openers. */
const CITATION_OPENER =
  /\bIbid\.?\b|\bCf\.?\b|\bop\. cit\.|\bSee\b|\b\p{Lu}[\p{L}'’-]+[,]?\s+\d{4}\b/u;

/** Mostly digits and punctuation: "KrV, A51/B75.", "pp. 12-20." */
function dominatedByDigitsAndPunctuation(text: string): boolean {
  const dense = text.replace(/\s+/g, "");
  if (dense.length === 0) return false;
  let bare = 0;
  for (const character of dense) {
    if (!/\p{L}/u.test(character)) bare++;
  }
  return bare / dense.length >= 0.5;
}

/** How many sentences a text holds: prose runs to more than one. */
const SENTENCE_ENDS = /[.!?](?=\s|$)/gu;

/** The deterministic shape classification of one footnote's text. */
function classifyShape(text: string): "drop" | "defer" | "classify" {
  const trimmed = text.trim();
  if (
    trimmed.length <= SHORT_FOOTNOTE ||
    CITATION_OPENER.test(trimmed) ||
    dominatedByDigitsAndPunctuation(trimmed)
  ) {
    return "drop";
  }
  const sentences = trimmed.match(SENTENCE_ENDS)?.length ?? 0;
  if (trimmed.length >= LONG_FOOTNOTE && sentences >= 2) return "defer";
  return "classify";
}

/** The segments of each page group, in order: a pageless segment joins its predecessor's. */
function pageGroups(segments: readonly PlanSegment[]): Map<number, number[]> {
  const groups = new Map<number, number[]>();
  let page = 0;
  segments.forEach((segment, index) => {
    if (typeof segment.pageIndex === "number") page = segment.pageIndex;
    const group = groups.get(page);
    if (group === undefined) groups.set(page, [index]);
    else group.push(index);
  });
  return groups;
}

export function planFootnotes(
  segments: readonly PlanSegment[],
  policy: FootnotePolicy,
  options: FootnoteOptions = {},
): FootnotePlan {
  const tags: FootnoteTag[] = segments.map(() => "body");
  const drops: number[] = [];
  const band = options.bottomBand ?? BOTTOM_BAND;
  const footnote = (index: number): void => {
    if (policy === "drop_all") tags[index] = "drop";
    else if (policy === "defer_all") tags[index] = "defer";
    else tags[index] = classifyShape(segments[index]!.text);
    if (tags[index] === "drop") drops.push(index);
  };

  for (const indices of pageGroups(segments).values()) {
    const ys = indices
      .map((index) => segments[index]!.firstRectY)
      .filter((y): y is number => typeof y === "number");
    const covered = ys.length > 0;
    const threshold = ys.length > 0 ? Math.min(...ys) + band * (Math.max(...ys) - Math.min(...ys)) : 0;
    // The in-band test a segment with geometry passes; a segment without y
    // is never in the band: no geometry, no guess.
    const inBand = (index: number): boolean => {
      const y = segments[index]!.firstRectY;
      return typeof y === "number" && y <= threshold;
    };

    if (covered) {
      // Geometry mode: a marker-led paragraph starting in the bottom band is
      // a candidate, and non-anchor paragraphs after one still in the band
      // are its continuations (footnotes wrap).
      let run = false;
      for (const index of indices) {
        const segment = segments[index]!;
        if (segment.anchor === "paragraphStart") {
          if (inBand(index) && hasMarker(segment.text)) {
            run = true;
            footnote(index);
          } else {
            run = false;
          }
        } else if (run && inBand(index)) footnote(index);
        else if (!inBand(index)) run = false;
      }
    } else {
      // No rect coverage for the page: marker-only, and corroborated -- a
      // run of sequentially numbered marker-led paragraphs down the page
      // tail, before the next body paragraph. A lone marker-led paragraph
      // ("12 of them were counted here.", "1994 was the year...") is body.
      let members: number[] = [];
      let tails: number[] = [];
      let last = 0;
      const close = (): void => {
        if (members.length >= 2) for (const index of [...members, ...tails]) footnote(index);
        members = [];
        tails = [];
      };
      for (const index of indices) {
        const segment = segments[index]!;
        if (segment.anchor === "paragraphStart") {
          const number = hasMarker(segment.text) ? markerNumber(segment.text) : null;
          if (number !== null && members.length > 0 && number === last + 1) {
            last = number;
            members.push(index);
          } else if (number !== null) {
            close();
            last = number;
            members = [index];
          } else {
            close();
          }
        } else if (members.length > 0) {
          tails.push(index);
        }
      }
      close();
    }
  }
  drops.sort((a, b) => a - b);
  return { tags, drops };
}
