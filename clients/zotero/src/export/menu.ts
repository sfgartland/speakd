// Which PDF an "Export audiobook" menu entry would export, from the items
// selected in the library.
//
// Pure over a lookup, so it needs nothing of Zotero but the shape of an item.

/** What this module needs of an item. */
export interface MenuItem {
  readonly id: number;
  readonly isAttachment: boolean;
  readonly contentType: string | null;
  /** A regular item (an article, a book): not an attachment or a note. */
  readonly isRegular: boolean;
  readonly parentID: number | null;
  /** A regular item's non-trashed attachments. */
  readonly childIDs: readonly number[];
  readonly itemType?: string;
}

export interface ExportTarget {
  /** The PDF attachment whose text is exported. */
  readonly attachmentID: number;
  /** The item the audiobook is titled and attached by: the PDF's parent, or the PDF itself. */
  readonly itemID: number;
}

const isPdf = (item: MenuItem): boolean => item.isAttachment && item.contentType === "application/pdf";

/**
 * The target for a selection: one PDF attachment, or one regular item with
 * exactly one PDF child. Anything else (nothing, several items, a book with
 * two PDFs, a snapshot) is null, and the menu entry stays disabled.
 */
export function exportTarget(
  selected: readonly MenuItem[],
  lookup: (id: number) => MenuItem | null,
): ExportTarget | null {
  if (selected.length !== 1) return null;
  const item = selected[0]!;
  if (isPdf(item)) return { attachmentID: item.id, itemID: item.parentID ?? item.id };
  if (!item.isRegular) return null;
  const pdfs = item.childIDs.map(lookup).filter((child): child is MenuItem => child !== null && isPdf(child));
  return pdfs.length === 1 ? { attachmentID: pdfs[0]!.id, itemID: item.id } : null;
}
