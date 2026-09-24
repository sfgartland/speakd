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

/** The rules, in the order they run: whitespace last, since the others leave gaps. */
export const RULES: readonly Rule[] = [joinHyphenation, dropPageNumbers, collapseWhitespace];

/** Every rule over `text`, with one map from the result back to `text`. */
export function cleanup(text: string, rules: readonly Rule[] = RULES): Stage {
  let stage: Stage = { text, map: identity(text.length) };
  for (const rule of rules) {
    const next = apply(stage.text, rule(stage.text));
    stage = { text: next.text, map: compose(stage.map, next.map) };
  }
  return stage;
}
