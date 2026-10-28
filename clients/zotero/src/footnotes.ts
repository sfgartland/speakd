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

import { filterRunningHeads } from "./segmentfilter";
import type { LabelCategory } from "./labelstore";
import type { TaggedSegment } from "./sections";

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
  /** The paragraph anchor; a segment that carries none is never a candidate. */
  readonly anchor?: string | null;
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

/** Every footnote marker the taxonomy names: superscripts, digits (OCR-confused
 * or not), n-digits, symbols. A lone confusable letter ('I ', 'l ') is not a
 * marker -- otherwise body paragraphs would be candidates on geometry alone. */
const MARKER =
  /^(?:[¹²³⁰-⁹]{1,3}|[0-9OolIil]{0,1}\d[0-9OolIil]{0,1}[.:\s]|[OolIil]{2,3}[.:\s]|n\d{1,2}|\*+|[†‡¶])/u;
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
  const canonical = text.slice(0, 3).replace(/[Oo]/gu, "0").replace(/[Ili]/gu, "1");
  const plain = /^(?:\d{1,2}|n(\d{1,2}))/.exec(canonical);
  if (plain === null) return null;
  return Number(plain[1] ?? plain[0]);
}

/**
 * What a bottom-band segment's marker is, for the second signal geometry
 * alone does not give (no font size reaches us): `symbol` markers (a
 * superscript, *, a dagger) are unambiguous; a `number` is the digits a
 * marker spells, with `lookalike` set when any of them was a letter OCR'd
 * from a digit ('Oil', 'Ill'), and `listy` when it ends in a full stop or
 * colon, as an enumerated body line does.
 */
interface MarkerInfo {
  readonly symbol: boolean;
  readonly number: number | null;
  readonly lookalike: boolean;
  readonly listy: boolean;
}

function markerInfo(text: string): MarkerInfo {
  const head = MARKER.exec(text.trimStart())?.[0] ?? "";
  if (/^(?:[¹²³⁰-⁹]|\*|[†‡¶])/u.test(head)) return { symbol: true, number: null, lookalike: false, listy: false };
  const lookalike = /[OolIil]/u.test(head);
  const digits = head.replace(/[Oo]/gu, "0").replace(/[Ili]/gu, "1").replace(/\D/gu, "");
  return {
    symbol: false,
    number: digits === "" ? null : Number(digits),
    lookalike,
    listy: /[.:]\s*$/u.test(head),
  };
}

/** The numbers body text cites as reference markers: a digit run stuck to the word or punctuation before it ("claim.3", "so,4", "word³"). */
function referenceMarkers(texts: readonly string[]): Set<number> {
  const found = new Set<number>();
  for (const text of texts) {
    for (const match of text.matchAll(/[\p{L}.,;:!?)"”’'](\d{1,3})(?![\d\p{L}])/gu)) found.add(Number(match[1]));
    for (const match of text.matchAll(/[¹²³⁰-⁹]{1,3}/gu)) {
      let value = 0;
      for (const character of match[0]) value = value * 10 + SUPERSCRIPT.indexOf(character);
      found.add(value);
    }
  }
  return found;
}

/** Whether the marker opens a segment's text. */
const hasMarker = (text: string): boolean => MARKER.test(text.trimStart());

/** A citation's shape, when the text holds one of its openers. */
const CITATION_OPENER =
  /\bIbid\.?\b|\bCf\.?\b|\bop\. cit\.|\bSee\b|\b\p{Lu}[\p{L}'’-]+[,]?\s+\d{4}\b/u;

/** A citation's closing locator: "Kant, KrV, B25.", "Heidegger, p. 65." */
const CITATION_LOCATOR = /(?:,|\bpp?\.)\s*[AB]?\d+(?:[/–-][AB]?\d+)?\.?\s*$/u;

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

/** Why one segment was dropped, for the log line a caller writes. */
export interface DroppedSegment {
  readonly text: string;
  readonly why: "running head" | "footnote" | "label";
}

/** One borderline footnote the batch classifier is asked about, by its original index. */
export interface ClassifyResidue {
  readonly index: number;
  readonly text: string;
}

/**
 * A document's `classify` footnotes, with their original indices: the export
 * path's batch input (Task 11). Empty under `drop_all`/`defer_all`, so the
 * export makes no call there either.
 */
export function classificationResidue<S extends PlanSegment>(
  segments: readonly S[],
  policy: FootnotePolicy,
): ClassifyResidue[] {
  const { tagged } = tagSegments(segments, policy);
  const residue: ClassifyResidue[] = [];
  tagged.forEach((segment, index) => {
    if (segment.tag === "classify") residue.push({ index, text: segment.text });
  });
  return residue;
}

/** Both filters over a whole document's segments, for whoever builds sections or parts. */
export interface SegmentsPlan<S extends PlanSegment> {
  /** The whole list, tagged for `buildSections`: a dropped one never appears in a section, a deferred one waits for its section's end. */
  readonly tagged: readonly (S & Pick<TaggedSegment, "tag">)[];
  /** Every dropped segment, in document order: the filter's audit trail. */
  readonly dropped: readonly DroppedSegment[];
}

/** The footnote policy's word for a detected candidate, shared by detection and labels. */
function footnoteTag(text: string, policy: FootnotePolicy): FootnoteTag {
  if (policy === "drop_all") return "drop";
  if (policy === "defer_all") return "defer";
  return classifyShape(text);
}

/** A whole-segment label's tag: the label feeds detection, the policy decides fate. */
function overrideTag(category: LabelCategory, text: string, policy: FootnotePolicy): FootnoteTag {
  switch (category) {
    case "body":
    case "heading":
      return "body";
    case "footnote":
      return footnoteTag(text, policy);
    default:
      // running-head / footer / page-number / junk: dropped, and audited.
      return "drop";
  }
}

/**
 * The running-head filter and the footnote plan over a whole document's
 * segments (proposal §4.2), in that order: the head filter first, so the
 * footnote plan sees what is left. Under `classify`, what the shape rules
 * could not decide is tagged `classify` and held back by `buildSections`
 * exactly as a `defer` is -- read at its section's end -- until a semantic
 * stage resolves it: a wrong kept footnote is preferable to a wrong dropped
 * paragraph. Nothing here drops by content; only recurrence, geometry,
 * markers and the shape rules decide.
 *
 * `overrides` is the labeler's per-capture map, keyed by original document
 * index (spec §8.2). It is applied to the whole list after both filters, so
 * index-keying survives the head filter's subsetting: a `body` label
 * restores a segment the head filter dropped, a `footnote` label makes a
 * segment a candidate and lets the policy decide its fate, and a drop label
 * removes a segment the heuristics would have kept. A label never touches
 * characters; the worst it can do is drop or keep whole speech, the same
 * blast radius the filters already have.
 */
export function tagSegments<S extends PlanSegment>(
  segments: readonly S[],
  policy: FootnotePolicy,
  overrides?: ReadonlyMap<number, LabelCategory>,
): SegmentsPlan<S> {
  const kept = filterRunningHeads(segments);
  // The kept list holds the input's own segment objects, so identity tells
  // kept from dropped; each kept one knows its place in the kept list.
  const keptAt = new Map<PlanSegment, number>();
  kept.forEach((segment, index) => keptAt.set(segment, index));
  const plan = planFootnotes(kept, policy);
  const tagged: (S & Pick<TaggedSegment, "tag">)[] = [];
  const dropped: DroppedSegment[] = [];
  segments.forEach((segment, documentIndex) => {
    const override = overrides?.get(documentIndex);
    if (override !== undefined) {
      const tag = overrideTag(override, segment.text, policy);
      if (tag === "drop") dropped.push({ text: segment.text, why: "label" });
      tagged.push({ ...segment, tag });
      return;
    }
    const index = keptAt.get(segment);
    if (index === undefined) {
      dropped.push({ text: segment.text, why: "running head" });
      tagged.push({ ...segment, tag: "drop" });
      return;
    }
    const tag = plan.tags[index] ?? "body";
    if (tag === "drop") dropped.push({ text: segment.text, why: "footnote" });
    tagged.push({ ...segment, tag });
  });
  return { tagged, dropped };
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
    tags[index] = footnoteTag(segments[index]!.text, policy);
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
      //
      // Marker and band alone are not enough for a digit marker: a body line
      // or a short numbered list item ("2. The second premise is false.")
      // can sit in the band too. A second structural signal must agree --
      // the body above cites the same number as a reference marker, or a
      // neighbouring note in the band numbers one off it, or the text has a
      // citation's own shape. A marker OCR'd from letters ('Oil', 'Ill')
      // needs one of the first two; a list-style "N." marker, one of the
      // first and the last. No signal, no footnote: kept as body.
      const bodyRefs = referenceMarkers(
        indices
          .filter((index) => !(inBand(index) && segments[index]!.anchor === "paragraphStart" && hasMarker(segments[index]!.text)))
          .map((index) => segments[index]!.text),
      );
      const bandNumbers = new Set<number>();
      for (const index of indices) {
        const segment = segments[index]!;
        if (segment.anchor === "paragraphStart" && inBand(index) && hasMarker(segment.text)) {
          const number = markerInfo(segment.text).number;
          if (number !== null) bandNumbers.add(number);
        }
      }
      const corroborated = (text: string): boolean => {
        const marker = markerInfo(text);
        if (marker.symbol) return true;
        const number = marker.number;
        if (number === null) return false;
        if (bodyRefs.has(number)) return true;
        if (marker.lookalike) return bandNumbers.has(number - 1) || bandNumbers.has(number + 1);
        const sequential = !marker.listy && (bandNumbers.has(number - 1) || bandNumbers.has(number + 1));
        return sequential || CITATION_OPENER.test(text) || CITATION_LOCATOR.test(text.trim()) || dominatedByDigitsAndPunctuation(text.trim());
      };
      let run = false;
      for (const index of indices) {
        const segment = segments[index]!;
        if (segment.anchor === "paragraphStart") {
          if (inBand(index) && hasMarker(segment.text) && corroborated(segment.text)) {
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
