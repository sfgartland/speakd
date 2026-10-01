// What the export dialog offers and what it answers, as plain data: the
// settings it starts from, the ranges an item can be exported by, and the
// choice a confirmed dialog hands on (to become a `render` request).
//
// Pure: the dialog window (addon/content/export-dialog.xhtml) only shows
// this module's state and feeds the user's edits back into it.

import { estimateSeconds, formatEstimate, DEFAULT_RTF } from "./estimate";
import { outlineToChapters, type ChapterRange } from "./outline";
import { buildParts, isBookItemType, type PartSegment, type PartsSelection } from "./parts";
import { parsePageRanges } from "./range";
import type { PdfStructure } from "./structure";

export type ExportFormat = "mp3" | "opus" | "m4b";
export type ExportDestination = "attach" | "folder";
export type RangeMode = "whole" | "chapters" | "pages";

const FORMATS: readonly ExportFormat[] = ["mp3", "opus", "m4b"];
const DESTINATIONS: readonly ExportDestination[] = ["attach", "folder"];

/** The `export_*` preferences (addon/prefs.js), parsed. */
export interface ExportSettings {
  readonly formatArticle: ExportFormat;
  readonly formatBook: ExportFormat;
  readonly destination: ExportDestination;
  /** Where "folder" exports go; empty means ask. */
  readonly folder: string;
  readonly skipReferences: boolean;
}

export const DEFAULT_SETTINGS: ExportSettings = {
  formatArticle: "mp3",
  formatBook: "m4b",
  destination: "attach",
  folder: "",
  skipReferences: true,
};

function oneOf<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.find((candidate) => candidate === value) ?? fallback;
}

/** The settings from raw preference values, whatever they hold: anything invalid is its default. */
export function parseSettings(raw: {
  formatArticle?: unknown;
  formatBook?: unknown;
  destination?: unknown;
  folder?: unknown;
  skipReferences?: unknown;
}): ExportSettings {
  return {
    formatArticle: oneOf(FORMATS, raw.formatArticle, DEFAULT_SETTINGS.formatArticle),
    formatBook: oneOf(FORMATS, raw.formatBook, DEFAULT_SETTINGS.formatBook),
    destination: oneOf(DESTINATIONS, raw.destination, DEFAULT_SETTINGS.destination),
    folder: typeof raw.folder === "string" ? raw.folder.trim() : DEFAULT_SETTINGS.folder,
    skipReferences: typeof raw.skipReferences === "boolean" ? raw.skipReferences : DEFAULT_SETTINGS.skipReferences,
  };
}

/** What the dialog needs to know of the item being exported. */
export interface DialogItem {
  readonly itemType: string;
  readonly title: string;
  /** The outline's chapters; null or empty when the PDF has none. */
  readonly chapters: readonly ChapterRange[] | null;
  /** The document's page count, or null if unknown (page numbers are then not checked). */
  readonly pageCount: number | null;
}

/** The dialog's item from the item's type and title and what the PDF itself holds. */
export function dialogItem(itemType: string, title: string, structure: PdfStructure): DialogItem {
  const chapters = outlineToChapters(structure.outline, structure.pageCount - 1);
  // An entry on the same page as the next one has no pages of its own: a
  // part heading, say, above its first chapter.
  const own = chapters?.filter((chapter) => chapter.endPage >= chapter.startPage) ?? [];
  return { itemType, title, chapters: own.length > 0 ? own : null, pageCount: structure.pageCount };
}

function hasChapters(item: DialogItem): boolean {
  return item.chapters !== null && item.chapters.length > 0;
}

/**
 * The ways this item can be ranged, the default first. A book with an
 * outline gets its chapters; one without gets a page range only, since an
 * empty chapter list would offer nothing.
 */
export function availableModes(item: DialogItem): RangeMode[] {
  if (isBookItemType(item.itemType)) return hasChapters(item) ? ["chapters", "pages", "whole"] : ["pages"];
  return ["whole", "pages"];
}

const MATTER_PATTERN =
  /^(contents|table of contents|copyright|copyright page|title page|half[- ]?title|dedication|epigraph|acknowledg(e)?ments?|index|bibliography|about the authors?|colophon|front matter|back matter)$/i;

/** A chapter that is front or back matter, which an audiobook does not want read out. */
export function isFrontBackMatter(title: string): boolean {
  return MATTER_PATTERN.test(title.trim());
}

/** The dialog's editable state. */
export interface DialogState {
  readonly mode: RangeMode;
  /** One per chapter of the item, in order. */
  readonly chapterChecks: readonly boolean[];
  /** The page-range field, as typed. */
  readonly pages: string;
  readonly skipReferences: boolean;
  readonly format: ExportFormat;
  readonly destination: ExportDestination;
  readonly folder: string;
}

/** The dialog's state before the user touches anything, from the item type and the settings. */
export function initialState(item: DialogItem, settings: ExportSettings): DialogState {
  return {
    mode: availableModes(item)[0] ?? "pages",
    chapterChecks: (item.chapters ?? []).map((chapter) => !isFrontBackMatter(chapter.title)),
    pages: "",
    skipReferences: settings.skipReferences,
    format: isBookItemType(item.itemType) ? settings.formatBook : settings.formatArticle,
    destination: settings.destination,
    folder: settings.folder,
  };
}

/**
 * What a confirmed dialog answers: the selection to build parts from
 * (export/parts.ts `buildParts`), and how and where to render them.
 */
export interface ExportChoice {
  readonly selection: PartsSelection;
  readonly format: ExportFormat;
  readonly destination: ExportDestination;
  /** The folder to leave the file in; null when attaching. */
  readonly folder: string | null;
}

export type ResolveResult =
  | { readonly ok: true; readonly choice: ExportChoice }
  | { readonly ok: false; readonly error: string };

function failure(error: string): ResolveResult {
  return { ok: false, error };
}

function selectionFor(state: DialogState, item: DialogItem): PartsSelection | string {
  if (state.mode === "whole") return { kind: "whole", skipReferences: state.skipReferences };

  if (state.mode === "chapters") {
    const chosen = (item.chapters ?? []).filter((_, index) => state.chapterChecks[index] === true);
    return chosen.length === 0 ? "Tick at least one chapter." : { kind: "chapters", chapters: chosen };
  }

  if (state.pages.trim() === "") return "Enter the pages to export, such as \"12-48, 60\".";
  const parsed = parsePageRanges(state.pages);
  if (!parsed.ok) return parsed.error;
  if (item.pageCount !== null && parsed.ranges.some((range) => range.endPage >= item.pageCount!)) {
    return `The document has ${item.pageCount} pages.`;
  }
  return { kind: "pages", ranges: parsed.ranges };
}

/** The choice the state amounts to, or what is wrong with it, worded for the dialog. */
export function resolveChoice(state: DialogState, item: DialogItem): ResolveResult {
  const selection = selectionFor(state, item);
  if (typeof selection === "string") return failure(selection);
  if (state.destination === "folder" && state.folder.trim() === "") return failure("Choose a folder.");
  return {
    ok: true,
    choice: {
      selection,
      format: state.format,
      destination: state.destination,
      folder: state.destination === "folder" ? state.folder.trim() : null,
    },
  };
}

export type DescribeResult =
  | { readonly ok: true; readonly estimate: string }
  | { readonly ok: false; readonly error: string };

/**
 * The dialog's estimate line for the state: how long rendering what is
 * selected should take, or the reason there is nothing to render.
 * `rtf` is the daemon's measured real-time factor, if it has one.
 */
export function describeState(
  segments: readonly PartSegment[],
  item: DialogItem,
  state: DialogState,
  rtf = DEFAULT_RTF,
): DescribeResult {
  const resolved = resolveChoice({ ...state, destination: "attach" }, item);
  if (!resolved.ok) return resolved;
  const parts = buildParts(segments, item.title, resolved.choice.selection);
  const characters = parts.reduce((total, part) => total + part.text.length, 0);
  if (characters === 0) return { ok: false, error: "The selection has no text." };
  return { ok: true, estimate: formatEstimate(estimateSeconds(characters, 1, rtf)) };
}
