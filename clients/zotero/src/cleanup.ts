// The deterministic cleanup stage: what a PDF's text layer leaves behind that
// should not be read aloud, removed by rules rather than by a model.
//
// Each rule is a function from text to a `Stage`, so its offset map comes
// with it and goes through `apply`'s check like any other stage's. The rules
// are deliberately narrow. A rule that fires where it should not changes
// what is said, and the listener has no way to see it happen; one that
// misses leaves a stray "12" or "hyphen- ation" to be heard, which is merely
// untidy.

import { apply, compose, diff, identity, type Stage } from "./offsetmap";

export type Rule = (text: string) => Stage;

/**
 * Rewrite every match of `pattern` in `text` with `replace`, and build the
 * offset map as it goes: characters outside a match map to themselves, and
 * each match is mapped by a diff of the match against its replacement,
 * which is a few characters long and so exact and cheap.
 */
function rewrite(text: string, pattern: RegExp, replace: (match: RegExpExecArray) => string): Stage {
  const out: string[] = [];
  const map: number[] = [];
  let from = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index;
    for (let i = from; i < start; i++) map.push(i);
    out.push(text.slice(from, start));
    const replacement = replace(match as RegExpExecArray);
    const local = diff(match[0], replacement);
    if (local === null) return { text, map: identity(text.length) };
    // The local map's last entry is the match's end, which is the next
    // character's to push, not one of the replacement's.
    for (const entry of local.slice(0, -1)) map.push(start + entry);
    out.push(replacement);
    from = start + match[0].length;
  }
  for (let i = from; i < text.length; i++) map.push(i);
  map.push(text.length);
  out.push(text.slice(from));
  return apply(text, { text: out.join(""), map });
}

// Words a suspended hyphen stands before: "pre- and post-war", "Vor- und
// Nachteile". Joining across one of these would say "preand".
const CONJUNCTIONS = ["and", "or", "nor", "to", "und", "oder", "bis", "sowie", "bzw"];

/**
 * Join a word the layout hyphenated across a line break: "hyphen-\nation",
 * or "hyphen- ation" once Zotero has turned the break into a space. Only
 * between two letters with a lowercase one after, so "X- Ray" and
 * "1990- 1995" stay as they are.
 */
export const joinHyphenation: Rule = (text) =>
  rewrite(
    text,
    new RegExp(`(?<=\\p{L})-\\s+(?=\\p{Ll})(?!(?:${CONJUNCTIONS.join("|")})\\b)`, "gu"),
    () => "",
  );

/** Collapse every run of whitespace to one space, and trim both ends. */
export const collapseWhitespace: Rule = (text) =>
  rewrite(text, /^\s+|\s+$|\s{2,}|[^\S ]/g, (match) =>
    match.index === 0 || match.index + match[0].length === text.length ? "" : " ",
  );

// What may close a sentence before a page number: a full stop, question or
// exclamation mark, perhaps followed by a closing quote or bracket.
const SENTENCE_END = `[.!?]["'”’)\\]]?`;

/**
 * Drop a page number left in the running text: a bare number of up to four
 * digits standing between two sentences, at the very start of the text
 * before a capitalised word, or at its very end. A number inside a sentence
 * ("in 1994", "12 of them") is never where one of these stands.
 */
export const dropPageNumbers: Rule = (text) =>
  rewrite(
    text,
    new RegExp(`(?<=^|${SENTENCE_END}\\s+)\\d{1,4}\\s+(?=\\p{Lu})|(?<=${SENTENCE_END})\\s+\\d{1,4}\\s*$`, "gu"),
    () => "",
  );

// Broken ligatures and their expansions (proposal §7: unambiguous codepoint
// substitutions). Everything else the ligature rule matches is removed: the
// zero-width, control and BOM characters and the stray marks (soft hyphen,
// ¬, ·) a scan's text layer leaves behind. Tabs and line breaks are not the
// ligature rule's to touch: collapseWhitespace handles whitespace.
const LIGATURE_EXPANSION: Record<string, string> = {
  "ﬀ": "ff",
  "ﬁ": "fi",
  "ﬂ": "fl",
  "ﬃ": "ffi",
  "ﬄ": "ffl",
};
const LIGATURE_OR_JUNK =
  /[ﬀﬁﬂﬃﬄ\u0000-\u0008\u000E-\u001F\u007F\u00AD\u00AC\u00B7\u200B-\u200D\u2060\uFEFF]/gu;

/** Expand broken ligatures and delete zero-width, control, BOM and stray scan marks. */
export const fixLigatures: Rule = (text) =>
  rewrite(text, LIGATURE_OR_JUNK, (match) => LIGATURE_EXPANSION[match[0]] ?? "");

/**
 * The rules, in the order they run: ligatures first, so words are whole
 * before they are joined; whitespace last, since the others leave gaps.
 */
export const RULES: readonly Rule[] = [fixLigatures, joinHyphenation, dropPageNumbers, collapseWhitespace];

// What an OCR-suspicion score counts (proposal §4.2, §5.3): the marks the
// ligature rule fixes, a digit run broken by one space ("in 1 994" -- never
// a range's "12- 20", whose space does not sit between digits), and the share
// of one- and two-letter tokens among word tokens ("rn", "cl", the torn
// halves of confusion pairs). Deterministic; what the score decides is only
// whether a closer look is worth it -- the OCR pass's gate, the skill's
// `--ocr-report` -- never a rewrite. The cut-off is provisional, not part of
// this contract.
const SPLIT_DIGITS = /(?<=\d)\s+(?=\d)/gu;
const SPLIT_DIGIT_WEIGHT = 2;
const SHORT_TOKEN_WEIGHT = 4;

/**
 * How much a text looks like a scan's OCR layer: 0 for clean prose, more the
 * more marks it carries. The exact cut-off is provisional until a real scan
// fixture arrives; what holds now is the ordering.
 */
export function ocrSuspicion(text: string): number {
  let score = 0;
  for (const _ of text.matchAll(LIGATURE_OR_JUNK)) score += 1;
  for (const _ of text.matchAll(SPLIT_DIGITS)) score += SPLIT_DIGIT_WEIGHT;
  const tokens = text.match(/\p{L}+/gu) ?? [];
  const short = tokens.filter((token) => token.length <= 2).length;
  return score + (tokens.length === 0 ? 0 : (SHORT_TOKEN_WEIGHT * short) / tokens.length);
}

/** Every rule over `text`, with one map from the result back to `text`. */
export function cleanup(text: string, rules: readonly Rule[] = RULES): Stage {
  let stage: Stage = { text, map: identity(text.length) };
  for (const rule of rules) {
    const next = apply(stage.text, rule(stage.text));
    stage = { text: next.text, map: compose(stage.map, next.map) };
  }
  return stage;
}
