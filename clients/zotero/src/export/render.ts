// Turning a confirmed export into the daemon's `render` request, and
// choosing the file it writes.
//
// Pure: paths are plain strings joined with "/" (speakd runs on Linux, and
// checks `out` itself: absolute, inside the home directory, with the
// format's extension).

import type { ExportFormat } from "./dialog";
import type { ExportRequest } from "./entry";
import { DEFAULT_CHARS_PER_SECOND } from "./estimate";
import { buildParts, type Part } from "./parts";

/** The `render` verb's payload. */
export interface RenderPayload {
  readonly parts: readonly Part[];
  readonly out: string;
  readonly format: ExportFormat;
  /** The pdf profile: the sentence-level pronunciation rules a paper wants. */
  readonly profile: "pdf";
  readonly metadata: Readonly<Record<string, string>>;
}

/** Tags read off the item, beyond its title. */
export interface ItemTags {
  readonly artist?: string;
  readonly date?: string;
}

/** The render request for an export, or null when its selection has no text to render. */
export function renderPayload(request: ExportRequest, out: string, tags: ItemTags): RenderPayload | null {
  const parts = buildParts(request.segments, request.title, request.choice.selection);
  if (parts.length === 0) return null;
  const metadata: Record<string, string> = { title: request.title, album: request.title };
  if (tags.artist) metadata.artist = tags.artist;
  if (tags.date) metadata.date = tags.date;
  return { parts, out, format: request.choice.format, profile: "pdf", metadata };
}

/**
 * Roughly how many seconds of audio the parts come to. The daemon's own
 * `estimate_seconds` is used instead whenever it reports one.
 */
export function expectedAudioSeconds(parts: readonly Part[]): number {
  return parts.reduce((total, part) => total + part.text.length, 0) / DEFAULT_CHARS_PER_SECOND;
}

const MAX_STEM = 120;

/** A title as a file name's stem: no separators or characters other systems refuse, no leading dot. */
export function fileStem(title: string): string {
  const stem = title
    // eslint-disable-next-line no-control-regex
    .replace(/[/\\:*?"<>|\u0000-\u001f]/g, (match) => (/\s/.test(match) ? " " : "-"))
    .replace(/\s+/g, " ")
    .replace(/^[\s.]+/, "")
    .trim()
    .slice(0, MAX_STEM)
    .trim();
  return stem === "" ? "audiobook" : stem;
}

/** A temporary file for an export that will be attached; unique by item and time. */
export function stagingOut(dir: string, itemID: number, format: ExportFormat, now: number): string {
  return `${dir.replace(/\/+$/, "")}/${itemID}-${now}.${format}`;
}

/**
 * The file an export saved to a folder is written to: named after the
 * title, numbered rather than overwriting anything already there.
 */
export async function folderOut(
  folder: string,
  title: string,
  format: ExportFormat,
  exists: (path: string) => boolean | Promise<boolean>,
): Promise<string> {
  const base = `${folder.replace(/\/+$/, "")}/${fileStem(title)}`;
  let path = `${base}.${format}`;
  for (let n = 2; await exists(path); n++) path = `${base} (${n}).${format}`;
  return path;
}
