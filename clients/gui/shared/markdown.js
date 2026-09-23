/**
 * markdown.js — the utterance as it was written, and where each sentence is in it.
 *
 * Most of what the daemon speaks is an agent's markdown. The window shows it
 * rendered, and walks a highlight through it as it is spoken, which needs two
 * things a markdown library does not give: every rendered block has to know
 * the source range it came from, because that is what a segment's span points
 * into, and nothing here may become markup — this page runs with
 * `window.__TAURI__` in reach, and the text is whatever an agent wrote. So the
 * grammar is the one src/speakd/transforms/markdown.py already speaks, and the
 * DOM is built from `createElement` and text nodes alone.
 *
 * The markdown transform gives every sentence of a block the whole block's
 * span, because rewriting destroys the character correspondence. `locate()`
 * recovers the sentence inside its block by matching its spoken text against
 * the block's rendered text, letters and digits only — rendered text and
 * spoken text have both lost the markup, so they mostly agree on those.
 */

const FENCE = /^\s*(```|~~~)/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const RULE = /^\s*([-*_])\s*(\1\s*){2,}$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?[\s:|-]+\|?\s*$/;

/** Inline patterns, tried together; the earliest match wins. */
const INLINE = [
  { re: /`([^`]+)`/, tag: "code", recurse: false },
  { re: /\[([^\]]+)\]\(([^)]*)\)/, tag: "link", recurse: true },
  { re: /\*\*([^*]+)\*\*/, tag: "strong", recurse: true },
  { re: /(?<!\*)\*([^*]+)\*(?!\*)/, tag: "em", recurse: true },
  { re: /(?<![\w_])_([^_]+)_(?![\w_])/, tag: "em", recurse: true },
];

/** Append `text` to `parent` with inline markup rendered. */
function inline(parent, text) {
  let rest = text;
  while (rest) {
    let best = null;
    for (const rule of INLINE) {
      const m = rule.re.exec(rest);
      if (m && (best === null || m.index < best.m.index)) best = { rule, m };
    }
    if (!best) {
      parent.append(document.createTextNode(rest));
      return;
    }
    const { rule, m } = best;
    if (m.index) parent.append(document.createTextNode(rest.slice(0, m.index)));
    // A link is a span, never an <a>: this webview must not navigate, and a
    // URL an agent wrote is not something a click here should open.
    const el = document.createElement(rule.tag === "link" ? "span" : rule.tag);
    if (rule.tag === "link") {
      el.className = "link";
      el.title = m[2];
    }
    if (rule.recurse) inline(el, m[1]);
    else el.textContent = m[1];
    parent.append(el);
    rest = rest.slice(m.index + m[0].length);
  }
}

/** Split into lines, each with the source offset it starts at. */
function lines(text) {
  const out = [];
  let at = 0;
  for (const line of text.split("\n")) {
    out.push({ line, start: at, end: at + line.length });
    at += line.length + 1;
  }
  return out;
}

/**
 * Render `text`. Returns the root element and the blocks, in order, each with
 * the source range `[start, end)` it was rendered from and the element that
 * holds its text. A block is the unit a segment's span is matched against:
 * a paragraph, a heading, one list item, a quote, a code block, a table.
 */
export function renderMarkdown(text) {
  const root = document.createElement("div");
  root.className = "md";
  const blocks = [];
  const rows = lines(String(text ?? ""));
  let list = null; // the open <ul>/<ol>, while consecutive items continue it

  const add = (el, start, end, parent = root) => {
    parent.append(el);
    blocks.push({ start, end, el });
  };

  let i = 0;
  while (i < rows.length) {
    const { line, start } = rows[i];
    if (!line.trim()) {
      list = null;
      i++;
      continue;
    }

    if (FENCE.test(line)) {
      const marker = FENCE.exec(line)[1];
      let j = i + 1;
      while (j < rows.length && !rows[j].line.trim().startsWith(marker)) j++;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = rows.slice(i + 1, j).map((r) => r.line).join("\n");
      pre.append(code);
      const last = rows[Math.min(j, rows.length - 1)];
      add(pre, start, last.end);
      list = null;
      i = j + 1;
      continue;
    }

    let m;
    if ((m = HEADING.exec(line))) {
      const h = document.createElement("h" + m[1].length);
      inline(h, m[2]);
      add(h, start, rows[i].end);
      list = null;
      i++;
      continue;
    }

    if (RULE.test(line)) {
      // Nothing is spoken for a rule, so it is drawn but is not a block.
      root.append(document.createElement("hr"));
      list = null;
      i++;
      continue;
    }

    const bullet = BULLET.exec(line);
    const ordered = bullet ? null : ORDERED.exec(line);
    if (bullet || ordered) {
      const tag = bullet ? "UL" : "OL";
      if (!list || list.tagName !== tag) {
        list = document.createElement(tag.toLowerCase());
        root.append(list);
      }
      const li = document.createElement("li");
      inline(li, (bullet || ordered)[1]);
      add(li, start, rows[i].end, list);
      i++;
      continue;
    }
    list = null;

    if (QUOTE.test(line)) {
      let j = i;
      const parts = [];
      while (j < rows.length && QUOTE.test(rows[j].line)) parts.push(QUOTE.exec(rows[j].line)[1]), j++;
      const quote = document.createElement("blockquote");
      const p = document.createElement("p");
      inline(p, parts.join(" "));
      quote.append(p);
      add(quote, start, rows[j - 1].end);
      i = j;
      continue;
    }

    if (TABLE_ROW.test(line)) {
      let j = i;
      const table = document.createElement("table");
      while (j < rows.length && TABLE_ROW.test(rows[j].line)) {
        const row = rows[j].line;
        if (!TABLE_RULE.test(row)) {
          const tr = document.createElement("tr");
          for (const cell of row.trim().replace(/^\||\|$/g, "").split("|")) {
            const td = document.createElement("td");
            inline(td, cell.trim());
            tr.append(td);
          }
          table.append(tr);
        }
        j++;
      }
      add(table, start, rows[j - 1].end);
      i = j;
      continue;
    }

    // A paragraph: every following line that starts nothing else.
    let j = i + 1;
    while (
      j < rows.length &&
      rows[j].line.trim() &&
      !FENCE.test(rows[j].line) &&
      !HEADING.test(rows[j].line) &&
      !BULLET.test(rows[j].line) &&
      !ORDERED.test(rows[j].line) &&
      !QUOTE.test(rows[j].line) &&
      !TABLE_ROW.test(rows[j].line) &&
      !RULE.test(rows[j].line)
    ) {
      j++;
    }
    const p = document.createElement("p");
    inline(p, rows.slice(i, j).map((r) => r.line.trim()).join(" "));
    add(p, start, rows[j - 1].end);
    i = j;
  }
  return { root, blocks };
}

/** Every text node under `el`, in document order. */
function textNodes(el) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const out = [];
  while (walker.nextNode()) out.push(walker.currentNode);
  return out;
}

/** The text of `el` as its text nodes spell it: the offsets everything below uses. */
export function flatText(el) {
  return textNodes(el).map((n) => n.data).join("");
}

const WORD = /[\p{L}\p{N}]/u;

/** Letters and digits of `text`, case-folded, with where each came from. */
function normalise(text) {
  let norm = "";
  const at = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (WORD.test(ch)) {
      norm += ch.toLowerCase();
      at.push(i);
    }
  }
  return { norm, at };
}

/**
 * Where `spoken` is in `el`'s text, searching from flat offset `from`.
 *
 * `{ start, end }` as flat offsets, or null. When the whole sentence is not
 * there — the pronunciation transform rewrote a word, say — its first and last
 * sixteen letters are tried as anchors, which survives a rewrite in the
 * middle. Null means the caller should light the whole block.
 */
export function locate(el, spoken, from = 0) {
  const flat = flatText(el);
  const hay = normalise(flat);
  const needle = normalise(String(spoken ?? "")).norm;
  if (!needle) return null;
  let fromNorm = hay.at.findIndex((pos) => pos >= from);
  if (fromNorm === -1) return null;

  // The match ends on a letter; the sentence ends on its punctuation, and a
  // highlight that stops one character short of the full stop looks broken.
  const span = (a, b) => {
    let end = hay.at[b - 1] + 1;
    while (end < flat.length && /[.!?,;:)\]"'’”…]/.test(flat[end])) end++;
    return { start: hay.at[a], end };
  };
  const whole = hay.norm.indexOf(needle, fromNorm);
  if (whole !== -1) return span(whole, whole + needle.length);

  const k = Math.min(16, needle.length);
  const head = hay.norm.indexOf(needle.slice(0, k), fromNorm);
  if (head === -1) return null;
  const tail = hay.norm.indexOf(needle.slice(-k), head);
  if (tail === -1) return null;
  return span(head, tail + k);
}

/**
 * Wrap flat range `[start, end)` of `el` in `span.<cls>` elements, splitting
 * text nodes where the range cuts them. Returns the spans, for `unwrap()`.
 */
export function wrapRange(el, start, end, cls) {
  const spans = [];
  let at = 0;
  for (const node of textNodes(el)) {
    const len = node.data.length;
    const a = Math.max(start, at);
    const b = Math.min(end, at + len);
    if (a < b) {
      let target = node;
      if (a > at) target = target.splitText(a - at);
      if (b < at + len) target.splitText(b - a);
      const span = document.createElement("span");
      span.className = cls;
      target.replaceWith(span);
      span.append(target);
      spans.push(span);
    }
    at += len;
    if (at >= end) break;
  }
  return spans;
}

/** Undo `wrapRange()`: the text goes back where it was, re-joined. */
export function unwrap(spans) {
  const parents = new Set();
  for (const span of spans) {
    const parent = span.parentNode;
    if (!parent) continue;
    span.replaceWith(...span.childNodes);
    parents.add(parent);
  }
  for (const parent of parents) parent.normalize();
}

/** The flat offset of a caret at (`node`, `offset`) inside `el`, or -1. */
export function offsetAt(el, node, offset) {
  if (!el.contains(node)) return -1;
  let at = 0;
  for (const text of textNodes(el)) {
    if (text === node) return at + offset;
    at += text.data.length;
  }
  // The caret landed on an element rather than in text: count what precedes it.
  const range = document.createRange();
  range.setStart(el, 0);
  range.setEnd(node, offset);
  return range.toString().length;
}
