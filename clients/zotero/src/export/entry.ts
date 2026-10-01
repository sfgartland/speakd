// The "Export audiobook" menu entry on library items: capture the PDF's
// text, ask what to export, and hand the choice on.
//
// What follows the dialog (rendering, progress, attaching: export/exporter.ts)
// takes the `ExportRequest` this flow produces.

import { dialogItem, type ExportChoice } from "./dialog";
import { captureSegments, type CaptureHost, type CapturedSegment } from "./capture";
import { openExportDialog, type ExportDialogRequest } from "./dialog-window";
import { exportTarget, type ExportTarget, type MenuItem } from "./menu";
import { readStructure, type PdfStructure } from "./structure";
import { readExportSettings } from "../prefs";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const MENU_ID = "speakd-reader-export";
const LABEL = "Export audiobook…";

/** What the dialog's confirmed choice is turned into a render from. */
export interface ExportRequest {
  /** The item the audiobook is titled and attached by. */
  readonly itemID: number;
  readonly title: string;
  /** The PDF whose text it is. */
  readonly attachmentID: number;
  readonly segments: readonly CapturedSegment[];
  readonly choice: ExportChoice;
}

export interface ExportEntryOptions {
  pluginID: string;
  captureHost: CaptureHost<Any>;
  /** Where a confirmed request goes. */
  onRequest(request: ExportRequest): void | Promise<void>;
  /** Where a problem is shown: the capture found nothing to read, say. */
  onProblem(title: string, message: string): void;
  warn(message: string, error?: unknown): void;
  /** The daemon's measured real-time factor, for the dialog's estimate, if known. */
  rtf?(): number | undefined;
}

function menuItem(item: Any): MenuItem {
  const attachment = item.isAttachment();
  return {
    id: item.id,
    isAttachment: attachment,
    contentType: attachment ? item.attachmentContentType : null,
    isRegular: item.isRegularItem(),
    parentID: item.parentItemID || null,
    childIDs: item.isRegularItem() ? item.getAttachments() : [],
    itemType: item.itemType,
  };
}

const lookup = (id: number): MenuItem | null => {
  const item = Zotero.Items.get(id);
  return item ? menuItem(item) : null;
};

const targetOf = (items: readonly Any[] | undefined) => exportTarget((items ?? []).map(menuItem), lookup);

/** What the menu entry would export for this item alone, or null. */
export const targetFor = (itemID: number): ExportTarget | null => targetOf([Zotero.Items.get(itemID)].filter(Boolean));

export interface ExportFlowDeps extends Pick<ExportEntryOptions, "captureHost" | "onRequest" | "onProblem" | "warn"> {
  readonly title: string;
  readonly itemType: string;
  /** The page count and outline of a reader's PDF. */
  structure(reader: Any): Promise<PdfStructure>;
  /** Shows the dialog; null when cancelled. */
  openDialog(request: Omit<ExportDialogRequest, "settings">): Promise<ExportChoice | null>;
}

/**
 * Capture the target's text, ask what to export, and pass the choice on.
 * A capture that fails, or finds no text, is a problem shown and nothing
 * more: no dialog, no request. Never rejects.
 */
export async function exportFlow(target: ExportTarget, deps: ExportFlowDeps): Promise<void> {
  try {
    const captured = await captureSegments(target.attachmentID, deps.captureHost);
    if (!captured.ok) {
      deps.onProblem(deps.title, captured.problem.error);
      return;
    }
    if (captured.segments.every((segment) => segment.text.trim() === "")) {
      deps.onProblem(deps.title, "this document has no text to read");
      return;
    }
    const structure = await structureOf(target.attachmentID, captured.segments, deps.captureHost, deps.structure);
    const choice = await deps.openDialog({
      item: dialogItem(deps.itemType, deps.title, structure),
      segments: captured.segments,
    });
    if (choice === null) return;
    await deps.onRequest({
      itemID: target.itemID,
      title: deps.title,
      attachmentID: target.attachmentID,
      segments: captured.segments,
      choice,
    });
  } catch (error) {
    deps.warn("the audiobook export failed", error);
    deps.onProblem("Export audiobook", String(error));
  }
}

/** Registers the menu entry. Returns an unregister. */
export function registerExportEntry(options: ExportEntryOptions): () => void {
  let busy = false;

  const run = async (items: readonly Any[] | undefined): Promise<void> => {
    const target = targetOf(items);
    if (target === null || busy) return;
    busy = true;
    try {
      await exportItem(target, options);
    } finally {
      busy = false;
    }
  };

  // Zotero builds the item menu while it is already showing, so a plugin's
  // onShowing may come too late for the first opening; onShown always
  // follows. The label is set directly, not through Fluent.
  const refresh = (_event: Event, context: Any): void => {
    context.menuElem.setAttribute("label", LABEL);
    context.setVisible(true);
    context.setEnabled(targetOf(context.items) !== null);
  };
  const id = Zotero.MenuManager.registerMenu({
    menuID: MENU_ID,
    pluginID: options.pluginID,
    target: "main/library/item",
    menus: [
      {
        menuType: "menuitem",
        onShowing: refresh,
        onShown: refresh,
        onCommand: (_event, context) => {
          void run(context.items);
        },
      },
    ],
  });
  if (id === false) options.warn("the export menu could not be registered");
  return () => {
    Zotero.MenuManager.unregisterMenu(MENU_ID);
  };
}

/**
 * Runs the export flow for a target, through the real dialog, or through
 * `openDialog` instead (for scripting the flow from the console).
 */
export async function exportItem(
  target: ExportTarget,
  options: Omit<ExportEntryOptions, "pluginID">,
  openDialog?: ExportFlowDeps["openDialog"],
): Promise<void> {
  const item = Zotero.Items.get(target.itemID) as Any;
  await exportFlow(target, {
    title: String(item.getDisplayTitle()),
    itemType: item.itemType,
    captureHost: options.captureHost,
    structure: readStructure,
    openDialog:
      openDialog ?? ((request) => openExportDialog({ ...request, settings: readExportSettings(), rtf: options.rtf?.() })),
    onRequest: options.onRequest,
    onProblem: options.onProblem,
    warn: options.warn,
  });
}

/**
 * The PDF's page count and outline, from a reader open on it (the one the
 * user has, or one opened just for this and closed again). Without them
 * (the reader would not open) the page count is the last page with text and
 * there is no outline, so the dialog offers pages only.
 */
async function structureOf(
  attachmentID: number,
  segments: readonly CapturedSegment[],
  host: CaptureHost<Any>,
  read: (reader: Any) => Promise<PdfStructure>,
): Promise<PdfStructure> {
  const fallback = { pageCount: Math.max(0, ...segments.map((segment) => segment.pageIndex)) + 1, outline: null };
  let reader = host.find(attachmentID);
  const opened = reader === null;
  try {
    reader ??= await host.open(attachmentID);
    return reader === null ? fallback : await read(reader);
  } catch {
    return fallback;
  } finally {
    if (opened && reader !== null) host.close(reader);
  }
}
