// The label pane and the label-mode model (spec §6).
//
// The pane is a narrow column injected into the reader's own document, next
// to the style the takeover already writes. It lists the current page's
// captured segments with a chip seeded from the pipeline's own prediction,
// so labelling is mostly confirm-or-correct. Keys are pane-scoped: the
// listener sits on the pane itself, so the reader's own keys are never
// intercepted, and `Esc` is stopped there too.
//
// This module is pure DOM + data: the highlight and the label-mode
// controller live in reader-takeover.ts, which owns the Zotero internals.
// The pane calls back into `onSelect` for the highlight and writes labels
// through the injected `save`.

import { tagSegments, type FootnotePolicy } from "./footnotes";
import { locatorFor, type LabelCategory, type LabelFile, type LabelRecord } from "./labelstore";
import { filterRunningHeads } from "./segmentfilter";

/** The minimum shape the pane needs of a captured segment. */
export interface LabelledSegment {
  readonly text: string;
  readonly pageIndex: number | null;
  readonly anchor?: string | null;
  readonly firstRectY?: number | null;
}

/** The category keys (spec §6.2). */
export const KEY_CATEGORIES: Readonly<Record<string, LabelCategory>> = {
  b: "body",
  f: "footnote",
  h: "running-head",
  p: "page-number",
  k: "junk",
  g: "heading",
};

/**
 * The pipeline's own answer for each segment, as a chip seed: body, a
 * detected footnote (deferred or classified), or a running head the filter
 * dropped. It is a prediction, not ground truth -- the owner confirms or
 * corrects it.
 */
export function predictCategories(segments: readonly LabelledSegment[], policy: FootnotePolicy): LabelCategory[] {
  const { tagged } = tagSegments(segments, policy);
  const kept = new Set(filterRunningHeads(segments));
  return segments.map((segment, index) => {
    const tag = tagged[index]?.tag ?? "body";
    if (tag === "body") return "body";
    if (tag === "defer" || tag === "classify") return "footnote";
    // A drop is the head filter's or the footnote plan's; identity says which.
    return kept.has(segment) ? "footnote" : "running-head";
  });
}

/** The indices of the segments on one page, in document order. */
export function segmentsOnPage(segments: readonly LabelledSegment[], page: number): number[] {
  const indices: number[] = [];
  segments.forEach((segment, index) => {
    if (segment.pageIndex === page) indices.push(index);
  });
  return indices;
}

/** The two locators name the same segment (spec §7.2). */
function sameSegment(a: LabelRecord["locator"], b: LabelRecord["locator"]): boolean {
  if (a.page !== b.page) return false;
  if (a.textSha256 === b.textSha256) return true;
  return a.prefix === b.prefix && a.anchor === b.anchor;
}

const DISPLAY_CHARS = 120;

export interface LabelerOptions {
  document: Document;
  segments: readonly LabelledSegment[];
  policy: FootnotePolicy;
  /** The loaded label file, or an empty one. */
  file: LabelFile;
  /** Labels already matched to segments, by index: what the chips start from. */
  overrides?: ReadonlyMap<number, LabelCategory>;
  /** Labels that matched no segment, listed in the pane. */
  unmatched: readonly LabelRecord[];
  save(file: LabelFile): Promise<void>;
  now(): number;
  /** A segment was selected: drive Zotero's highlight and scroll to it. */
  onSelect(index: number): void;
  /** `Esc`: leave label mode. */
  onExit(): void;
  /** The "Export fixture…" button: write the labelled fixture somewhere local. */
  onExport?(): void;
  warn(message: string, error?: unknown): void;
}

/** The label-mode pane: a page's segments, their chips, and the label writes. */
export class Labeler {
  private readonly options: LabelerOptions;
  private readonly segments: readonly LabelledSegment[];
  private readonly prediction: LabelCategory[];
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly status: HTMLElement;
  private readonly onKey: (event: KeyboardEvent) => void;
  private currentFile: LabelFile;
  private readonly currentOverrides: Map<number, LabelCategory>;
  private _selected = 0;
  private _page = 0;
  private disposed = false;

  constructor(options: LabelerOptions) {
    this.options = options;
    this.segments = options.segments;
    this.prediction = predictCategories(options.segments, options.policy);
    this.currentFile = options.file;
    this.currentOverrides = new Map(options.overrides ?? []);
    this._page = options.segments[0]?.pageIndex ?? 0;
    this.root = options.document.createElement("div");
    this.root.className = "speakd-label-pane";
    this.root.tabIndex = 0;
    this.root.setAttribute("role", "region");
    this.root.setAttribute("aria-label", "speakd labels");

    const title = options.document.createElement("div");
    title.className = "speakd-label-title";
    title.textContent = "Label this document";
    if (options.onExport !== undefined) {
      const exportButton = options.document.createElement("button");
      exportButton.className = "speakd-label-export";
      exportButton.textContent = "Export fixture…";
      exportButton.title = "Write this document's ground-truth fixture";
      exportButton.addEventListener("click", () => options.onExport?.());
      title.appendChild(exportButton);
    }
    const close = options.document.createElement("button");
    close.className = "speakd-label-close";
    close.textContent = "×";
    close.title = "Exit label mode (Esc)";
    close.addEventListener("click", () => this.options.onExit());
    title.appendChild(close);
    this.status = options.document.createElement("div");
    this.status.className = "speakd-label-status";
    this.list = options.document.createElement("ol");
    this.list.className = "speakd-label-list";
    this.root.append(title, this.status, this.list);

    this.onKey = (event) => this.keydown(event);
    this.root.addEventListener("keydown", this.onKey);
  }

  /** The selected segment's index. */
  get selected(): number {
    return this._selected;
  }

  /** The page the pane is showing. */
  get page(): number {
    return this._page;
  }

  /** The label file as the pane has amended it. */
  get file(): LabelFile {
    return this.currentFile;
  }

  /** The categories written this session, by segment index. */
  get overrides(): ReadonlyMap<number, LabelCategory> {
    return new Map(this.currentOverrides);
  }

  /** Appends the pane to the reader document and draws it. */
  mount(): HTMLElement {
    this.options.document.documentElement.appendChild(this.root);
    this.render();
    return this.root;
  }

  /** Select a segment, on whichever page it sits, and drive its highlight. */
  select(index: number): void {
    if (index < 0 || index >= this.segments.length) return;
    this._selected = index;
    this._page = this.segments[index]!.pageIndex ?? this._page;
    this.render();
    this.options.onSelect(index);
  }

  /** Walk by `delta` segments, continuing across a page break. */
  move(delta: number): void {
    const next = Math.min(Math.max(this._selected + delta, 0), this.segments.length - 1);
    this.select(next);
  }

  /** Jump to the next segment with no label yet, wrapping once. */
  nextUnlabeled(): void {
    const total = this.segments.length;
    if (total === 0) return;
    for (let step = 1; step <= total; step++) {
      const index = (this._selected + step) % total;
      if (!this.currentOverrides.has(index)) {
        this.select(index);
        return;
      }
    }
  }

  /** Write one segment's category through the store and redraw its chip. */
  setCategory(index: number, category: LabelCategory): Promise<void> {
    const segment = this.segments[index];
    if (segment === undefined) return Promise.resolve();
    const record: LabelRecord = { locator: locatorFor(segment), category, labelledAt: this.options.now(), note: "" };
    const labels = this.currentFile.labels.filter((entry) => !sameSegment(entry.locator, record.locator));
    labels.push(record);
    this.currentFile = { ...this.currentFile, labels };
    this.currentOverrides.set(index, category);
    this.render();
    return Promise.resolve(this.options.save(this.currentFile)).catch((error) =>
      this.options.warn("saving the labels failed", error),
    );
  }

  /** Show a one-line note in the pane, until the next redraw. */
  note(message: string): void {
    this.status.textContent = message;
  }

  /** The pane's own key handling: pane-scoped, never the reader's. */
  keydown(event: KeyboardEvent): void {
    const category = KEY_CATEGORIES[event.key];
    if (category !== undefined) {
      event.stopPropagation();
      this.setCategory(this._selected, category);
      return;
    }
    if (event.key === "ArrowDown") {
      event.stopPropagation();
      this.move(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.stopPropagation();
      this.move(-1);
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      this.options.onExit();
    }
  }

  /** Take the pane out of the reader document. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.root.removeEventListener("keydown", this.onKey);
    this.root.remove();
  }

  private render(): void {
    const unmatched = this.options.unmatched.length;
    this.status.textContent =
      unmatched === 0
        ? ""
        : `${unmatched} label${unmatched === 1 ? "" : "s"} no longer match${unmatched === 1 ? "es" : ""} this document — re-label or discard`;
    this.list.replaceChildren();
    for (const index of segmentsOnPage(this.segments, this._page)) {
      const segment = this.segments[index]!;
      const item = this.options.document.createElement("li");
      item.className = "speakd-label-item";
      item.dataset.index = String(index);
      if (index === this._selected) item.classList.add("speakd-label-selected");
      item.addEventListener("click", () => this.select(index));

      const category = this.currentOverrides.get(index) ?? this.prediction[index] ?? "body";
      const chip = this.options.document.createElement("span");
      chip.className = "speakd-label-chip";
      chip.dataset.category = category;
      chip.textContent = category;

      const picker = this.options.document.createElement("select");
      picker.className = "speakd-label-picker";
      for (const option of Object.values(KEY_CATEGORIES)) {
        const entry = this.options.document.createElement("option");
        entry.value = option;
        entry.textContent = option;
        picker.appendChild(entry);
      }
      picker.value = category;
      picker.addEventListener("change", () => this.setCategory(index, picker.value as LabelCategory));

      const text = this.options.document.createElement("span");
      text.className = "speakd-label-text";
      text.textContent = segment.text.replace(/\s+/gu, " ").trim().slice(0, DISPLAY_CHARS);

      item.append(chip, picker, text);
      this.list.appendChild(item);
    }
  }
}

/** The pane shown while Zotero prepares a cold capture (spec §6.1). */
export interface CapturePane {
  readonly root: HTMLElement;
  setElapsed(seconds: number): void;
  fail(reason: string): void;
  dispose(): void;
}

/** A capturing pane: elapsed seconds, never a faked percentage; `Esc` abandons the wait. */
export function mountCapturePane(document: Document, onAbort: () => void): CapturePane {
  const root = document.createElement("div");
  root.className = "speakd-label-pane speakd-label-capturing";
  root.tabIndex = 0;
  const spinner = document.createElement("span");
  spinner.className = "speakd-label-spinner";
  spinner.textContent = "…";
  const message = document.createElement("p");
  message.textContent = "Zotero is preparing the document; this happens once per label-mode session.";
  const elapsed = document.createElement("p");
  elapsed.className = "speakd-label-elapsed";
  elapsed.textContent = "capturing… 0s";
  root.append(spinner, message, elapsed);

  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    onAbort();
  };
  root.addEventListener("keydown", onKey);
  document.documentElement.appendChild(root);

  return {
    root,
    setElapsed: (seconds) => {
      elapsed.textContent = `capturing… ${Math.max(0, Math.floor(seconds))}s`;
    },
    fail: (reason) => {
      elapsed.textContent = reason;
    },
    dispose: () => {
      root.removeEventListener("keydown", onKey);
      root.remove();
    },
  };
}
