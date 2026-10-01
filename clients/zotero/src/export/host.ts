// The exporter's Zotero side: files, attachments, notifications and the
// ledger's preference. Zotero glue only; the logic is export/exporter.ts.

import type { ExporterHost } from "./exporter";
import { fileStem, type ItemTags } from "./render";
import { PREF_PREFIX } from "../prefs";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const IOUtils: any;
declare const PathUtils: any;
type Any = any;

const LEDGER_PREF = `${PREF_PREFIX}render_jobs`;

/**
 * Where an export to be attached is rendered, before Zotero imports it.
 *
 * Not `PathUtils.tempDir`: Zotero runs as a flatpak here, whose /tmp is its
 * own and invisible to speakd, and speakd writes `out` only inside the home
 * directory anyway. The profile directory is in the home directory and
 * seen alike from inside the sandbox and out, and, unlike Zotero's own
 * temporary directory, nothing clears it at startup, so a render that
 * finishes while Zotero is closed is still there to be attached.
 */
function stagingPath(): string {
  return PathUtils.join(PathUtils.profileDir, "speakd-exports");
}

function itemTags(itemID: number): ItemTags {
  const item: Any = Zotero.Items.get(itemID);
  if (!item || !item.isRegularItem()) return {};
  const tags: { artist?: string; date?: string } = {};
  const artist = String(item.getField("firstCreator") ?? "").trim();
  const date = String(item.getField("year") ?? "").trim();
  if (artist !== "") tags.artist = artist;
  if (date !== "") tags.date = date;
  return tags;
}

export function notify(title: string, message: string): void {
  const progress = new Zotero.ProgressWindow({ closeOnClick: true });
  progress.changeHeadline(title);
  progress.addDescription(message);
  progress.show();
  progress.startCloseTimer(8000);
}

export function zoteroExporterHost(options: Pick<ExporterHost, "call" | "warn">): ExporterHost {
  return {
    call: options.call,
    warn: options.warn,
    loadLedger: () => Zotero.Prefs.get(LEDGER_PREF, true),
    saveLedger: (raw) => {
      Zotero.Prefs.set(LEDGER_PREF, raw, true);
    },
    stagingDir: async () => {
      const dir = stagingPath();
      await IOUtils.makeDirectory(dir, { ignoreExisting: true, createAncestors: true });
      return dir;
    },
    exists: (path) => IOUtils.exists(path),
    remove: (path) => IOUtils.remove(path, { ignoreAbsent: true }),
    attach: async (tracked, file) => {
      const item: Any = Zotero.Items.get(tracked.itemID);
      if (!item) throw new Error(`item ${tracked.itemID} no longer exists`);
      const common = { file, title: `Audio — ${tracked.title}`, fileBaseName: fileStem(tracked.title) };
      // A PDF with no parent item cannot have children: the audio goes
      // beside it, in its library, instead.
      await Zotero.Attachments.importFromFile(
        item.isRegularItem() ? { ...common, parentItemID: item.id } : { ...common, libraryID: item.libraryID },
      );
    },
    tags: itemTags,
    notify,
    now: () => Date.now(),
  };
}
