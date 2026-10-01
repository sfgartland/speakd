// Opening the export dialog (addon/content/export-dialog.xhtml) and
// waiting for its answer.
//
// The window gets this module's pure logic (dialog.ts) in its arguments, so
// it draws and edits the same state the tests cover.

import {
  availableModes,
  describeState,
  initialState,
  resolveChoice,
  type DialogItem,
  type ExportChoice,
  type ExportSettings,
} from "./dialog";
import type { PartSegment } from "./parts";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const ChromeUtils: any;
type Any = any;

/** Registered at startup (index.ts), since a window cannot be opened from the plugin's jar: URL. */
export const CHROME_PACKAGE = "speakd-reader";
const EXPORT_DIALOG_URL = `chrome://${CHROME_PACKAGE}/content/export-dialog.xhtml`;

export interface ExportDialogRequest {
  readonly item: DialogItem;
  readonly segments: readonly PartSegment[];
  readonly settings: ExportSettings;
  /** The daemon's measured real-time factor, if known. */
  readonly rtf?: number;
}

/** A folder chosen in the system's picker, or null if cancelled. */
async function pickFolder(parent: Any, current: string): Promise<string | null> {
  const { FilePicker } = ChromeUtils.importESModule("chrome://zotero/content/modules/filePicker.mjs");
  const picker = new FilePicker();
  picker.init(parent, "Folder for the audiobook", picker.modeGetFolder);
  if (current !== "") picker.displayDirectory = current;
  return (await picker.show()) === picker.returnOK ? String(picker.file) : null;
}

/**
 * Shows the dialog over the main window. Resolves with the choice when
 * Export is pressed, or null when the dialog is cancelled or closed.
 */
export function openExportDialog(request: ExportDialogRequest): Promise<ExportChoice | null> {
  return new Promise((resolve) => {
    let settled = false;
    const args = {
      ...request,
      availableModes,
      initialState,
      resolveChoice,
      describeState,
      pickFolder,
      done: (choice: ExportChoice | null) => {
        if (settled) return;
        settled = true;
        resolve(choice);
      },
    };
    const parent: Any = Zotero.getMainWindow();
    const opened = parent.openDialog(
      EXPORT_DIALOG_URL,
      "speakd-export",
      "chrome,dialog=no,centerscreen,resizable,width=520,height=620",
      args,
    );
    if (!opened) args.done(null);
  });
}
