// The ground-truth fixture export (spec §8.3, §8.5).
//
// Four files under a directory: the captured segments, the label file (the
// ground truth), a regression snapshot of what the shipped pipeline with
// the labels applied produces today, and a README with the source
// fingerprint, the reproduction command, the gap report and the copyright
// bound record. `fixtureContent` is pure; `writeFixture` is the IOUtils glue.

import { tagSegments, type FootnotePolicy } from "../footnotes";
import { serialiseLabelFile, type LabelCategory, type LabelFile } from "../labelstore";
import { buildSections } from "../sections";
import type { CapturedSegment } from "../session";
import { dumpSegments } from "./debugdump";
import { buildParts } from "./parts";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const IOUtils: any;
declare const PathUtils: any;

/** Where the pane's export button writes a fixture when no path is chosen: the profile, not the repo. */
export function defaultFixtureDir(attachmentKey: string): string {
  return PathUtils.join(PathUtils.profileDir, "speakd-reader", "fixtures", attachmentKey === "" ? "document" : attachmentKey);
}

/** The pages and text content a fixture may carry without the owner's sign-off (spec §8.5). */
export const FIXTURE_PAGE_BOUND = 8;
export const FIXTURE_TEXT_BOUND = 20 * 1024;

export interface FixtureInput {
  readonly segments: readonly CapturedSegment[];
  /** The label file verbatim: the ground truth. */
  readonly labels: LabelFile;
  readonly policy: FootnotePolicy;
  /** The label store's matched overrides, by original captured index. */
  readonly overrides: ReadonlyMap<number, LabelCategory>;
  /** The source PDF's path on the owner's machine; never the file. */
  readonly pdfPath?: string | null;
  /** The dropped-before-capture lines, or null when the gap check was unavailable. */
  readonly gaps?: readonly string[] | null;
  /** The harness command that reproduces the fixture. */
  readonly command?: string;
}

export interface FixtureOutput {
  readonly "capture.jsonl": string;
  readonly "labels.json": string;
  readonly "expected.json": string;
  readonly "README.md": string;
}

/** The whitespace-collapsed readable prose the fixture exposes, as bytes. */
function textContentBytes(text: string): number {
  return new TextEncoder().encode(text.replace(/\s+/gu, " ").trim()).length;
}

/** The four fixture files, as strings. */
export function fixtureContent(input: FixtureInput): FixtureOutput {
  const { tagged } = tagSegments(input.segments, input.policy, input.overrides);
  const sections = buildSections(tagged, 0);
  const parts = buildParts(
    input.segments,
    input.labels.attachmentKey || "Document",
    { kind: "whole" },
    { footnotes: input.policy, overrides: input.overrides },
  );
  const expected = {
    schema: 1,
    tags: tagged.map((segment) => segment.tag ?? "body"),
    sections: sections.map((section) => ({ segmentIndices: section.segmentIndices, text: section.text })),
    parts: parts.map((part) => ({ title: part.title, text: part.text })),
  };
  const pages = new Set(input.segments.map((segment) => segment.pageIndex)).size;
  const contentText = [
    ...input.segments.map((segment) => segment.text),
    ...sections.map((section) => section.text),
  ].join(" ");
  const gapLines =
    input.gaps == null
      ? ["gap check unavailable"]
      : input.gaps.length === 0
        ? ["None."]
        : input.gaps.map((gap) => `- ${gap}`);
  const readme = [
    `# Fixture ${input.labels.attachmentKey}`,
    "",
    "Derived artifacts only; the source PDF is never checked in (spec §8.5).",
    "",
    `- Source path: ${input.pdfPath ?? "(not recorded)"}`,
    `- sha256: ${input.labels.documentSha256}`,
    `- Pages: ${pages} (bound: ${FIXTURE_PAGE_BOUND})`,
    `- Text content: ${textContentBytes(contentText)} bytes (bound: ~${FIXTURE_TEXT_BOUND})`,
    `- Labels: ${input.labels.labels.length}`,
    "",
    "## Reproduce",
    "",
    "```",
    input.command ?? `Zotero.SpeakdReader.handles().find(...).exportFixture("<dir>")`,
    "```",
    "",
    "## Capture gaps (dropped before the capture; unlabelable by construction)",
    "",
    ...gapLines,
    "",
  ].join("\n");

  return {
    "capture.jsonl": dumpSegments(input.segments),
    "labels.json": serialiseLabelFile(input.labels),
    "expected.json": `${JSON.stringify(expected, null, 2)}\n`,
    "README.md": readme,
  };
}

/** Writes the four files into `dir`, creating it if need be. Never rejects. */
export async function writeFixture(
  dir: string,
  content: FixtureOutput,
): Promise<{ readonly ok: true; readonly path: string } | { readonly ok: false; readonly error: string }> {
  try {
    const base = String(dir).replace(/\/+$/u, "");
    await IOUtils.makeDirectory(base, { ignoreExisting: true, createAncestors: true });
    for (const [name, text] of Object.entries(content)) {
      await IOUtils.writeUTF8(`${base}/${name}`, text);
    }
    return { ok: true, path: base };
  } catch (error) {
    return { ok: false, error: `writing the fixture failed: ${String(error)}` };
  }
}
