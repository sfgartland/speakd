// Character offset maps: which input character each output character of a
// text-rewriting stage came from.
//
// Every stage between Zotero's segments and speakd's spans rewrites text, and
// speakd reports what it is saying as spans into the text it was given. To
// highlight the right sentence, a span has to be carried back through every
// stage to the segment it started in. A wrong map is worse than no cleanup at
// all -- it highlights the wrong sentence -- so every map is verified, and a
// stage whose map fails is dropped for its input, unchanged.

/**
 * `map[i]` is the input index output character `i` came from, and the last
 * entry, `map[output.length]`, is the input's length, so that the end of a
 * span maps as well as its start. An inserted character maps to the input
 * position of the next character kept, which keeps the map monotone.
 */
export type OffsetMap = readonly number[];

/** A stage's result: the rewritten text and where each of its characters came from. */
export interface Stage {
  readonly text: string;
  readonly map: OffsetMap;
}

/** The map of a stage that changed nothing. */
export function identity(length: number): number[] {
  return Array.from({ length: length + 1 }, (_, i) => i);
}

/** Whether `map` could describe a rewrite of an `inLen`-character text into an `outLen`-character one. */
export function verify(map: OffsetMap, inLen: number, outLen: number): boolean {
  if (map.length !== outLen + 1 || map[outLen] !== inLen) return false;
  let previous = 0;
  for (const entry of map) {
    if (!Number.isInteger(entry) || entry < previous || entry > inLen) return false;
    previous = entry;
  }
  return true;
}

/**
 * The map of two stages run one after the other: `first` from input to
 * middle, `second` from middle to output. Both must already have verified.
 */
export function compose(first: OffsetMap, second: OffsetMap): number[] {
  return second.map((middle) => first[middle]!);
}

/** The input index output index `index` came from, clamped to the map's ends. */
export function lookup(map: OffsetMap, index: number): number {
  const clamped = Math.min(Math.max(index, 0), map.length - 1);
  return map[clamped]!;
}

/**
 * The stage itself when its map verifies against its input, and otherwise
 * the input, unchanged: the fallback every stage has, so that a rule or a
 * model that got its bookkeeping wrong costs its cleanup and nothing more.
 */
export function apply(input: string, stage: Stage): Stage {
  if (verify(stage.map, input.length, stage.text.length)) return stage;
  return { text: input, map: identity(input.length) };
}

/**
 * A stage for a rewrite whose map nobody wrote down -- an LLM's answer --
 * recovered by diffing the two texts, with `apply`'s fallback.
 */
export function stageFromDiff(input: string, output: string, maxEdits?: number): Stage {
  const map = diff(input, output, maxEdits);
  if (map === null) return { text: input, map: identity(input.length) };
  return apply(input, { text: output, map });
}

type Edit = "keep" | "delete" | "insert";

// A section is about 1800 characters, and a cleanup that edits more than this
// many of them has not made minimal edits; it has rewritten the text. The
// bound also bounds the diff's memory, which grows with the square of it.
const DEFAULT_MAX_EDITS = 2000;

/**
 * The offset map from `input` to `output`, from a character-level Myers
 * diff, or null when the two differ by more than `maxEdits` insertions and
 * deletions.
 */
export function diff(input: string, output: string, maxEdits = DEFAULT_MAX_EDITS): number[] | null {
  // What the two share at either end needs no search, and for a cleanup
  // stage that is nearly all of it.
  let prefix = 0;
  const shorter = Math.min(input.length, output.length);
  while (prefix < shorter && input[prefix] === output[prefix]) prefix++;
  let suffix = 0;
  while (
    suffix < shorter - prefix &&
    input[input.length - 1 - suffix] === output[output.length - 1 - suffix]
  ) {
    suffix++;
  }
  const middle = myers(
    input.slice(prefix, input.length - suffix),
    output.slice(prefix, output.length - suffix),
    maxEdits,
  );
  if (middle === null) return null;

  const map: number[] = [];
  let from = 0;
  const walk = (edit: Edit): void => {
    if (edit === "keep") map.push(from++);
    else if (edit === "delete") from++;
    else map.push(from);
  };
  for (let i = 0; i < prefix; i++) walk("keep");
  for (const edit of middle) walk(edit);
  for (let i = 0; i < suffix; i++) walk("keep");
  map.push(input.length);
  return map;
}

/** The shortest edit script turning `a` into `b`, or null past `maxEdits` edits. */
function myers(a: string, b: string, maxEdits: number): Edit[] | null {
  const n = a.length;
  const m = b.length;
  const limit = Math.min(n + m, maxEdits);
  const offset = limit + 1;
  // v[offset + k] is the furthest x reached on diagonal k = x - y.
  const v = new Int32Array(2 * limit + 3);
  // Before each round d, the part of v that round reads, [-d-1, d+1], so the
  // path can be walked back afterwards. Keeping only that window, rather than
  // all of v, is what keeps memory to about limit squared.
  const trace: Int32Array[] = [];

  for (let d = 0; d <= limit; d++) {
    trace.push(v.slice(offset - d - 1, offset + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x =
        k === -d || (k !== d && v[offset + k - 1]! < v[offset + k + 1]!)
          ? v[offset + k + 1]!
          : v[offset + k - 1]! + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) {
        x++;
        y++;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) return backtrack(trace, n, m);
    }
  }
  return null;
}

function backtrack(trace: Int32Array[], n: number, m: number): Edit[] {
  const edits: Edit[] = [];
  let x = n;
  let y = m;
  for (let d = trace.length - 1; d >= 0; d--) {
    const window = trace[d]!;
    // The window starts at diagonal -d-1.
    const at = (k: number): number => window[k + d + 1]!;
    const k = x - y;
    const previousK = k === -d || (k !== d && at(k - 1) < at(k + 1)) ? k + 1 : k - 1;
    const previousX = at(previousK);
    const previousY = previousX - previousK;
    while (x > previousX && y > previousY) {
      edits.push("keep");
      x--;
      y--;
    }
    if (d > 0) edits.push(x === previousX ? "insert" : "delete");
    x = previousX;
    y = previousY;
  }
  return edits.reverse();
}
