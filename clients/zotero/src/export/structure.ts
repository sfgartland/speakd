// A PDF's outline as page indices, read from pdf.js in a reader.
//
// `resolveOutline` is pure over the slice of pdf.js's document it needs;
// `readStructure` is that document in Zotero.

import type { OutlineItem } from "./outline";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

/** A top-level entry of pdf.js's `getOutline()`. */
export interface RawOutlineItem {
  readonly title: string;
  /** A named destination, an explicit one ([ref-or-page, ...]), or null (a URL, say). */
  readonly dest: string | readonly unknown[] | null;
  readonly items?: readonly unknown[];
}

/** What a destination needs of pdf.js's PDFDocumentProxy. */
export interface OutlineDocument {
  getDestination(name: string): Promise<readonly unknown[] | null>;
  getPageIndex(ref: unknown): Promise<number>;
}

async function pageOf(dest: RawOutlineItem["dest"], doc: OutlineDocument): Promise<number | null> {
  try {
    const explicit = typeof dest === "string" ? await doc.getDestination(dest) : dest;
    const target = explicit?.[0];
    if (typeof target === "number") return target;
    if (target === null || target === undefined) return null;
    return await doc.getPageIndex(target);
  } catch {
    // A destination pdf.js cannot place is an entry that cannot be exported by.
    return null;
  }
}

/**
 * The top-level outline entries with their page indices, in page order.
 * Entries that cannot be placed or have no title are dropped; no outline,
 * or none left, is null.
 */
export async function resolveOutline(
  raw: readonly RawOutlineItem[] | null | undefined,
  doc: OutlineDocument,
): Promise<OutlineItem[] | null> {
  if (!raw || raw.length === 0) return null;
  const resolved: OutlineItem[] = [];
  for (const entry of raw) {
    const title = String(entry.title ?? "").trim();
    const pageIndex = await pageOf(entry.dest, doc);
    if (title !== "" && pageIndex !== null) resolved.push({ title, pageIndex });
  }
  // Array.prototype.sort is stable, so entries on one page keep their order.
  resolved.sort((a, b) => a.pageIndex - b.pageIndex);
  return resolved.length === 0 ? null : resolved;
}

export interface PdfStructure {
  readonly pageCount: number;
  readonly outline: OutlineItem[] | null;
}

/** The page count and outline of the PDF open in `reader`. */
export async function readStructure(reader: Any): Promise<PdfStructure> {
  await reader._initPromise;
  const view = reader._internalReader._primaryView;
  await view.initializedPromise;
  const doc = view._iframeWindow.PDFViewerApplication.pdfDocument;
  return { pageCount: doc.numPages, outline: await resolveOutline(await doc.getOutline(), doc) };
}
