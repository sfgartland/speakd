// The renders the plugin has started, as plain data: what the daemon says
// of each (`status.render.jobs`, the `render` event), the ledger that
// remembers them across a Zotero restart, and how a job's state reads in
// the item pane.
//
// The daemon's jobs do not say which item they are for, so the plugin
// keeps its own ledger (a preference) of the jobs it started. On the next
// start it is matched against `status`, which is how a render that went on
// while Zotero was closed shows its progress again, or is attached once
// found finished.

import { DEFAULT_RTF, formatEstimate } from "./estimate";
import type { ExportDestination, ExportFormat } from "./dialog";

export type JobState = "queued" | "running" | "paused" | "done" | "failed" | "cancelled";

const STATES: readonly JobState[] = ["queued", "running", "paused", "done", "failed", "cancelled"];
const FORMATS: readonly ExportFormat[] = ["mp3", "opus", "m4b"];
const DESTINATIONS: readonly ExportDestination[] = ["attach", "folder"];

/** A job as the daemon reports it (`RenderJob.state_dict`). */
export interface JobReport {
  readonly job: string;
  readonly state: JobState;
  /** The part being rendered, from 0; `parts` once all are done. */
  readonly part: number;
  readonly parts: number;
  /** Seconds of audio rendered so far. */
  readonly doneSeconds: number;
  /** Seconds of audio the daemon expects in all; 0 when it has no estimate. */
  readonly estimateSeconds: number;
  /** The finished file, once done. */
  readonly out: string | null;
  readonly error: string | null;
}

/** A render the plugin started, as its ledger remembers it. */
export interface TrackedJob {
  readonly job: string;
  /** The item the file is attached to (and titled by). */
  readonly itemID: number;
  /** The PDF it was read from, whose pane shows the progress too. */
  readonly attachmentID: number;
  readonly title: string;
  readonly format: ExportFormat;
  readonly destination: ExportDestination;
  /** Where the daemon was told to write it. */
  readonly out: string;
  /** Seconds of audio expected, from the text's length. */
  readonly expectedSeconds: number;
}

const number = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);
const text = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

/** A job from a `render` event's data or a `status.render.jobs` entry, or null if it is not one. */
export function parseJobReport(data: unknown): JobReport | null {
  if (typeof data !== "object" || data === null) return null;
  const raw = data as Record<string, unknown>;
  const job = text(raw.job);
  const state = STATES.find((candidate) => candidate === raw.state);
  if (job === null || state === undefined) return null;
  return {
    job,
    state,
    part: number(raw.part),
    parts: number(raw.parts),
    doneSeconds: number(raw.done_seconds),
    estimateSeconds: number(raw.estimate_seconds),
    out: text(raw.out),
    error: text(raw.error),
  };
}

function parseTracked(value: unknown): TrackedJob | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  const format = FORMATS.find((candidate) => candidate === raw.format);
  const destination = DESTINATIONS.find((candidate) => candidate === raw.destination);
  if (
    text(raw.job) === null ||
    !Number.isInteger(raw.itemID) ||
    !Number.isInteger(raw.attachmentID) ||
    typeof raw.title !== "string" ||
    format === undefined ||
    destination === undefined ||
    text(raw.out) === null
  ) {
    return null;
  }
  return {
    job: raw.job as string,
    itemID: raw.itemID as number,
    attachmentID: raw.attachmentID as number,
    title: raw.title,
    format,
    destination,
    out: raw.out as string,
    expectedSeconds: number(raw.expectedSeconds),
  };
}

/** The ledger from its preference value; whatever cannot be read is left out. */
export function parseLedger(raw: unknown): TrackedJob[] {
  if (typeof raw !== "string") return [];
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(value)) return [];
  return value.map(parseTracked).filter((tracked): tracked is TrackedJob => tracked !== null);
}

export function serializeLedger(jobs: readonly TrackedJob[]): string {
  return JSON.stringify(jobs);
}

/** What to do about a remembered job, given what the daemon says now. */
export type Pickup =
  | { readonly kind: "watch"; readonly tracked: TrackedJob; readonly report: JobReport }
  | { readonly kind: "finish"; readonly tracked: TrackedJob; readonly out: string }
  | { readonly kind: "fail"; readonly tracked: TrackedJob; readonly error: string }
  | { readonly kind: "drop"; readonly tracked: TrackedJob }
  /** Not in `status`: a daemon restarted after it finished, or one that lost it. */
  | { readonly kind: "missing"; readonly tracked: TrackedJob };

/** The decision for each remembered job, against the daemon's `status.render.jobs`. */
export function pickUp(tracked: readonly TrackedJob[], reports: readonly JobReport[]): Pickup[] {
  return tracked.map((entry): Pickup => {
    const report = reports.find((candidate) => candidate.job === entry.job);
    if (report === undefined) return { kind: "missing", tracked: entry };
    switch (report.state) {
      case "done":
        return { kind: "finish", tracked: entry, out: report.out ?? entry.out };
      case "failed":
        return { kind: "fail", tracked: entry, error: report.error ?? "the render failed" };
      case "cancelled":
        return { kind: "drop", tracked: entry };
      default:
        return { kind: "watch", tracked: entry, report };
    }
  });
}

/** A job's state as the item pane shows it. */
export interface ProgressView {
  readonly headline: string;
  /** 0-100, or null where a bar would mean nothing. */
  readonly percent: number | null;
  readonly detail: string;
}

/**
 * How a job reads in the item pane. `rate` is the wall-clock seconds a
 * second of this render's audio has been taking, once measured; until
 * then the time left assumes the default real-time factor.
 */
export function describeProgress(tracked: TrackedJob, report: JobReport | null, rate: number | null): ProgressView {
  if (report === null) return { headline: "Waiting for speakd", percent: null, detail: "" };
  const total = report.estimateSeconds > 0 ? report.estimateSeconds : tracked.expectedSeconds;
  // Short of done, never 100: the estimate is only from the text's length.
  const share =
    total > 0 ? Math.min(99, Math.max(0, Math.floor((100 * report.doneSeconds) / total))) : 0;
  const left = Math.max(0, total - report.doneSeconds) * (rate ?? DEFAULT_RTF);
  switch (report.state) {
    case "queued":
      return { headline: "Waiting to render", percent: 0, detail: "" };
    case "running": {
      const part = report.parts > 1 ? ` part ${Math.min(report.part + 1, report.parts)} of ${report.parts}` : "";
      return { headline: `Rendering${part}`, percent: share, detail: total > 0 ? `${formatEstimate(left)} left` : "" };
    }
    case "paused":
      return { headline: "Paused while speakd speaks", percent: share, detail: "" };
    case "done":
      return { headline: "Done", percent: 100, detail: "" };
    case "failed":
      return { headline: "Failed", percent: null, detail: report.error ?? "" };
    case "cancelled":
      return { headline: "Cancelled", percent: null, detail: "" };
  }
}
