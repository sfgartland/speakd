// The label store: one JSON file per attachment, keyed by segment content
// and place rather than array position (spec §7). A label is ground truth
// for what a captured segment *is*; this module only reads, matches and
// writes it. It never edits text and never touches the pipeline.
//
// Pure except for the two IOUtils wrappers at the bottom, which follow the
// `stagingPath` pattern (`export/host.ts:26-27`): the profile directory is
// seen alike under the flatpak sandbox and out, and survives a Zotero
// restart. Writes are whole-file and atomic.

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const IOUtils: any;
declare const PathUtils: any;

/** The taxonomy the pane offers (spec §7.1). */
export type LabelCategory = "body" | "footnote" | "heading" | "running-head" | "footer" | "page-number" | "junk";

/** Where a segment was when it was labelled: content and place, not an index. */
export interface LabelLocator {
  /** `pageIndex`, predecessor-inherited as the capture places it. */
  readonly page: number | null;
  /** `firstRectY`, or null when the segment carried no rects. */
  readonly rectY: number | null;
  /** The paragraph anchor, or null. */
  readonly anchor: string | null;
  /** The first 48 characters of the whitespace-collapsed text. */
  readonly prefix: string;
  /** sha256 of the full whitespace-collapsed text. */
  readonly textSha256: string;
}

/** One labelled segment. */
export interface LabelRecord {
  readonly locator: LabelLocator;
  readonly category: LabelCategory;
  /** Unix seconds, as the spec's example. */
  readonly labelledAt: number;
  /** Free text; where capture-gap suspicions are recorded. */
  readonly note: string;
}

/** The exact JSON of spec §7.1: one file per attachment. */
export interface LabelFile {
  readonly schema: 1;
  readonly attachmentKey: string;
  readonly documentSha256: string;
  readonly capture: {
    readonly zoteroVersion: string;
    readonly segments: number;
    readonly labelledAt: number;
  };
  readonly labels: readonly LabelRecord[];
}

export type ParsedLabelFile = { readonly ok: true; readonly file: LabelFile } | { readonly ok: false; readonly why: string };

/** A label file with no labels: what a fresh label-mode session starts from. */
export function emptyLabelFile(attachmentKey: string, segments: number, zoteroVersion: string, labelledAt: number, documentSha256 = ""): LabelFile {
  return {
    schema: 1,
    attachmentKey,
    documentSha256,
    capture: { zoteroVersion, segments, labelledAt },
    labels: [],
  };
}

/** Whitespace collapsed to single spaces, ends trimmed: what locators key on. */
export function collapseText(text: string): string {
  return text.replace(/\s+/gu, " ").trim();
}

/** The prefix length spec §7.1 fixes: 48 characters of the collapsed text. */
export const PREFIX_LENGTH = 48;

// ---- sha256 (sync, pure JS) ----
//
// The plugin runs in Zotero, where `crypto.subtle` is async and would put a
// promise between a capture and its override map. Matching is pure and must
// stay synchronous, so the hash is a small, dependency-free implementation
// that works unchanged in Zotero and under vitest.

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (value: number, bits: number): number => (value >>> bits) | (value << (32 - bits));

/** sha256 of `text`'s UTF-8 bytes, as lowercase hex. */
export function sha256Hex(text: string): string {
  return sha256Bytes(new TextEncoder().encode(text));
}

/** sha256 of a byte array, as lowercase hex. */
export function sha256Bytes(data: Uint8Array): string {
  const length = data.length;
  const padded = (((length + 9) >> 6) + 1) << 6;
  const bytes = new Uint8Array(padded);
  bytes.set(data);
  bytes[length] = 0x80;
  const view = new DataView(bytes.buffer);
  view.setUint32(padded - 8, Math.floor((length * 8) / 0x100000000));
  view.setUint32(padded - 4, (length * 8) >>> 0);

  const hash = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Uint32Array(64);
  for (let chunk = 0; chunk < padded; chunk += 64) {
    for (let i = 0; i < 16; i++) {
      const at = chunk + i * 4;
      w[i] = ((bytes[at]! << 24) | (bytes[at + 1]! << 16) | (bytes[at + 2]! << 8) | bytes[at + 3]!) >>> 0;
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15]!, 7) ^ rotr(w[i - 15]!, 18) ^ (w[i - 15]! >>> 3);
      const s1 = rotr(w[i - 2]!, 17) ^ rotr(w[i - 2]!, 19) ^ (w[i - 2]! >>> 10);
      w[i] = (w[i - 16]! + s0 + w[i - 7]! + s1) >>> 0;
    }
    let a = hash[0]!;
    let b = hash[1]!;
    let c = hash[2]!;
    let d = hash[3]!;
    let e = hash[4]!;
    let f = hash[5]!;
    let g = hash[6]!;
    let h = hash[7]!;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + choose + K[i]! + w[i]!) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    hash[0] = (hash[0]! + a) >>> 0;
    hash[1] = (hash[1]! + b) >>> 0;
    hash[2] = (hash[2]! + c) >>> 0;
    hash[3] = (hash[3]! + d) >>> 0;
    hash[4] = (hash[4]! + e) >>> 0;
    hash[5] = (hash[5]! + f) >>> 0;
    hash[6] = (hash[6]! + g) >>> 0;
    hash[7] = (hash[7]! + h) >>> 0;
  }
  return [...hash].map((word) => word.toString(16).padStart(8, "0")).join("");
}

/** The shape matching needs of a segment: what a locator keys on. */
export interface LabelableSegment {
  readonly text: string;
  readonly pageIndex?: number | null;
  readonly anchor?: string | null;
  readonly firstRectY?: number | null;
}

/** The locator for a captured segment (spec §7.1). */
export function locatorFor(segment: LabelableSegment): LabelLocator {
  const collapsed = collapseText(segment.text);
  return {
    page: segment.pageIndex ?? null,
    rectY: segment.firstRectY ?? null,
    anchor: segment.anchor ?? null,
    prefix: collapsed.slice(0, PREFIX_LENGTH),
    textSha256: sha256Hex(collapsed),
  };
}

export interface MatchResult {
  /** Original document index → category, for the one seam that applies labels. */
  readonly overrides: ReadonlyMap<number, LabelCategory>;
  /** Labels that matched no segment: inert, to be listed in the pane. */
  readonly unmatched: readonly LabelRecord[];
}

/**
 * Matches a file's labels against a capture's segments (spec §7.2). Exact
 * page + full-text hash wins; next, same page + same prefix + same anchor
 * (the text survived a re-segmentation that moved the boundaries around it);
 * anything else is unmatched and never applied. No fuzzy matching below an
 * exact prefix: a wrong hit would silently drop body speech, the one failure
 * worse than a stale label. A segment already matched is not re-matched.
 */
export function matchLabels(file: LabelFile, segments: readonly LabelableSegment[]): MatchResult {
  const collapsed = segments.map((segment) => collapseText(segment.text));
  const hashes = collapsed.map(sha256Hex);
  const prefixes = collapsed.map((text) => text.slice(0, PREFIX_LENGTH));
  const used = new Set<number>();
  const overrides = new Map<number, LabelCategory>();
  const unmatched: LabelRecord[] = [];

  for (const label of file.labels) {
    let found = -1;
    for (let index = 0; index < segments.length; index++) {
      if (used.has(index)) continue;
      if (segments[index]!.pageIndex === label.locator.page && hashes[index] === label.locator.textSha256) {
        found = index;
        break;
      }
    }
    if (found < 0) {
      for (let index = 0; index < segments.length; index++) {
        if (used.has(index)) continue;
        const anchor = segments[index]!.anchor ?? null;
        if (
          segments[index]!.pageIndex === label.locator.page &&
          prefixes[index] === label.locator.prefix &&
          anchor === label.locator.anchor
        ) {
          found = index;
          break;
        }
      }
    }
    if (found < 0) {
      unmatched.push(label);
      continue;
    }
    used.add(found);
    overrides.set(found, label.category);
  }
  return { overrides, unmatched };
}

/** The label file as JSON: pretty, so a human can read ground truth. */
export function serialiseLabelFile(file: LabelFile): string {
  return `${JSON.stringify(file, null, 2)}\n`;
}

/** Parses a label file; unparsable JSON or an unknown schema fails open (spec §10). */
export function parseLabelFile(json: string): ParsedLabelFile {
  let value: unknown;
  try {
    value = JSON.parse(json);
  } catch (error) {
    return { ok: false, why: `unparsable label file: ${String(error)}` };
  }
  if (typeof value !== "object" || value === null) return { ok: false, why: "the label file is not an object" };
  const candidate = value as Record<string, unknown>;
  if (candidate.schema !== 1) return { ok: false, why: `unknown label schema: ${String(candidate.schema)}` };
  if (!Array.isArray(candidate.labels)) return { ok: false, why: "the label file has no labels list" };
  if (!candidate.labels.every(isLabelRecord)) return { ok: false, why: "the label file has a malformed label record" };
  return { ok: true, file: value as LabelFile };
}

/** A locator with the fields matching reads: anything else fails the file open. */
function isLocator(value: unknown): value is LabelLocator {
  if (typeof value !== "object" || value === null) return false;
  const locator = value as Record<string, unknown>;
  return (
    (typeof locator.page === "number" || locator.page === null) &&
    typeof locator.prefix === "string" &&
    typeof locator.textSha256 === "string" &&
    (locator.anchor === undefined || locator.anchor === null || typeof locator.anchor === "string") &&
    (locator.rectY === undefined || locator.rectY === null || typeof locator.rectY === "number")
  );
}

/** A label record with a known category and a locator. */
function isLabelRecord(value: unknown): value is LabelRecord {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.category === "string" && isLocator(record.locator);
}

/** Where an attachment's label file lives (spec §7.3, the `stagingPath` pattern). */
export function labelsPath(attachmentKey: string): string {
  return PathUtils.join(PathUtils.profileDir, "speakd-reader", "labels", `${attachmentKey}.json`);
}

/**
 * Reads an attachment's label file. A missing or invalid file is no labels,
 * not a failure: the caller fails open and reads as if unlabelled.
 */
export async function readLabelFile(path: string): Promise<LabelFile | null> {
  let json: string;
  try {
    if (!(await IOUtils.exists(path))) return null;
    json = await IOUtils.readUTF8(path);
  } catch {
    return null;
  }
  const parsed = parseLabelFile(json);
  return parsed.ok ? parsed.file : null;
}

/** Writes an attachment's label file atomically: whole-file temp, then rename. */
export async function writeLabelFile(path: string, file: LabelFile): Promise<void> {
  const temp = `${path}.${Date.now()}.tmp`;
  await IOUtils.makeDirectory(PathUtils.parent(path), { ignoreExisting: true, createAncestors: true });
  await IOUtils.writeUTF8(temp, serialiseLabelFile(file));
  try {
    await IOUtils.move(temp, path);
  } catch (error) {
    await IOUtils.remove(temp, { ignoreAbsent: true });
    throw error;
  }
}
