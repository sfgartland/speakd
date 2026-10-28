// A dev-only segment dump for the Stage 0 characterisation
// (docs/design/2026-10-08-pdf-text-cleanup-stage0.md): one JSON line per
// captured segment, its geometry coverage among them, and one summary line
// per page. No shipped UI reaches it; the live-test bridge drives it
// (test-live/zeval.sh, `Zotero.SpeakdReader.handles()[i].dumpSegments(path)`).
//
// Pure. `dumpToFile` is the dev action itself: it captures through a handle
// and writes the dump where the harness asked.

import type { CaptureResult, CapturedSegment } from "../session";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const IOUtils: any;

/**
 * One JSON value per line for every segment: its place, its text, and its
 * geometry coverage -- `rectCount` and `firstRectY`, null when the segment
 * carries no rects. Then one summary line per page: how many segments it
 * holds, how many of them had rects, and the min/max first-rect y among
 * those that did.
 */
export function dumpSegments(segments: readonly CapturedSegment[]): string {
  const out: string[] = [];
  for (let index = 0; index < segments.length; index++) {
    const segment = segments[index]!;
    out.push(
      JSON.stringify({
        index,
        text: segment.text,
        pageIndex: segment.pageIndex,
        anchor: segment.anchor,
        rectCount: segment.rectCount,
        firstRectY: segment.firstRectY,
      }),
    );
  }
  const pages = new Map<number, { segments: number; withRects: number; lows: number[]; highs: number[] }>();
  for (const segment of segments) {
    const page = pages.get(segment.pageIndex) ?? { segments: 0, withRects: 0, lows: [], highs: [] };
    page.segments++;
    if (segment.firstRectY !== null) {
      page.withRects++;
      page.lows.push(segment.firstRectY);
    }
    pages.set(segment.pageIndex, page);
  }
  for (const [page, counts] of pages) {
    out.push(
      JSON.stringify({
        page,
        segments: counts.segments,
        withRects: counts.withRects,
        minFirstRectY: counts.lows.length ? Math.min(...counts.lows) : null,
        maxFirstRectY: counts.lows.length ? Math.max(...counts.lows) : null,
      }),
    );
  }
  return out.length === 0 ? "" : `${out.join("\n")}\n`;
}

export type DumpResult =
  | { readonly ok: true; readonly path: string; readonly segments: number }
  | { readonly ok: false; readonly error: string };

/**
 * The dev action behind the test bridge: capture the reader's segments and
 * write their dump to `path` (absolute; IOUtils insists). Never rejects.
 */
export async function dumpToFile(capture: () => Promise<CaptureResult>, path: string): Promise<DumpResult> {
  let result: CaptureResult;
  try {
    result = await capture();
  } catch (error) {
    return { ok: false, error: `capturing the segments failed: ${String(error)}` };
  }
  if (!result.ok) return { ok: false, error: result.problem.error };
  try {
    await IOUtils.writeUTF8(path, dumpSegments(result.segments));
  } catch (error) {
    return { ok: false, error: `writing the dump failed: ${String(error)}` };
  }
  return { ok: true, path, segments: result.segments.length };
}
