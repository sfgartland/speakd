// The item pane section that shows an audiobook render's progress, on the
// item and on the PDF it was read from: state, a bar, the time left, and
// Cancel. Shown only while one of the item's renders is under way.
//
// Zotero glue only; what it draws is export/jobs.ts `describeProgress`,
// through the exporter.

import type { Exporter } from "./exporter";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const PANE_ID = "speakd-reader-export";
const FTL = "speakd-reader-export.ftl";
const HTML = "http://www.w3.org/1999/xhtml";
const ICON = "chrome://zotero/skin/item-type/16/light/audio-recording.svg";
const DARK_ICON = "chrome://zotero/skin/item-type/16/dark/audio-recording.svg";

/** What a section's hooks last gave it: enough to show or hide it, and redraw it, from outside. */
interface Live {
  refresh(): Promise<void>;
  item: Any;
  setEnabled(enabled: boolean): void;
}

/** Registers the section. Returns an unregister. */
export function registerExportPane(options: {
  pluginID: string;
  exporter: Exporter;
  warn(message: string, error?: unknown): void;
}): () => void {
  const { exporter, warn } = options;
  const live = new Map<HTMLElement, Live>();

  const show = (item: Any, setEnabled: (enabled: boolean) => void): void => {
    setEnabled(item !== undefined && item !== null && exporter.jobsFor(item.id).length > 0);
  };

  const draw = (body: HTMLElement, item: Any): void => {
    const doc = body.ownerDocument;
    const element = (tag: string, className: string, text = ""): Any => {
      const node = doc.createElementNS(HTML, tag) as Any;
      node.className = className;
      if (text !== "") node.textContent = text;
      return node;
    };
    const rows = exporter.jobsFor(item?.id ?? -1).map(({ tracked, view }) => {
      const row = element("div", "speakd-export-job");
      row.style.cssText = "display: flex; flex-direction: column; gap: 4px; padding: 4px 0;";
      const percent = view.percent === null ? "" : ` — ${view.percent} %`;
      row.append(element("div", "speakd-export-headline", `${tracked.format.toUpperCase()}: ${view.headline}${percent}`));
      if (view.percent !== null) {
        const bar = element("progress", "speakd-export-bar");
        bar.max = 100;
        bar.value = view.percent;
        bar.style.width = "100%";
        row.append(bar);
      }
      if (view.detail !== "") row.append(element("div", "speakd-export-detail", view.detail));
      const cancel = element("button", "speakd-export-cancel", "Cancel");
      cancel.style.alignSelf = "flex-start";
      cancel.addEventListener("click", () => {
        cancel.disabled = true;
        exporter.cancel(tracked.job).catch((error) => warn("cancelling the render failed", error));
      });
      row.append(cancel);
      return row;
    });
    body.replaceChildren(...rows);
  };

  const unsubscribe = exporter.onChange(() => {
    for (const section of live.values()) {
      try {
        show(section.item, section.setEnabled);
        void section.refresh();
      } catch (error) {
        warn("refreshing the export pane failed", error);
      }
    }
  });

  const key = Zotero.ItemPaneManager.registerSection({
    paneID: PANE_ID,
    pluginID: options.pluginID,
    header: { l10nID: "speakd-reader-export-header", icon: ICON, darkIcon: DARK_ICON },
    sidenav: { l10nID: "speakd-reader-export-sidenav", icon: ICON, darkIcon: DARK_ICON },
    onInit: ({ body, item, setEnabled, refresh, doc }: Any) => {
      // Fluent strings for the header and the sidenav button; the plugin's
      // locale/ directory is registered by Zotero (plugins.js).
      (doc.defaultView as Any)?.MozXULElement?.insertFTLIfNeeded(FTL);
      live.set(body, { refresh, item, setEnabled });
    },
    onDestroy: ({ body }: Any) => {
      live.delete(body);
    },
    onItemChange: ({ body, item, setEnabled }: Any) => {
      const section = live.get(body);
      if (section !== undefined) {
        section.item = item;
        section.setEnabled = setEnabled;
      }
      show(item, setEnabled);
    },
    onRender: ({ body, item }: Any) => {
      draw(body, item);
    },
  });
  if (key === false) warn("the export progress section could not be registered");
  return () => {
    unsubscribe();
    live.clear();
    if (key !== false) Zotero.ItemPaneManager.unregisterSection(key);
  };
}
