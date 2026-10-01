// Rendering a confirmed export: send `render`, follow the job, and when it
// is done attach the file to its item or leave it in the chosen folder.
//
// The renders started are kept in a ledger (export/jobs.ts) that outlives
// Zotero, because the daemon's render goes on when Zotero is closed. On
// every connection to speakd, startup included, `sync` matches the ledger
// against `status` and picks up where things stand.
//
// Everything Zotero does (files, attachments, notifications, preferences)
// is behind `ExporterHost`, so this is tested with plain fakes.

import type { ExportRequest } from "./entry";
import {
  describeProgress,
  parseJobReport,
  parseLedger,
  pickUp,
  serializeLedger,
  type JobReport,
  type ProgressView,
  type TrackedJob,
} from "./jobs";
import { expectedAudioSeconds, folderOut, renderPayload, stagingOut, type ItemTags } from "./render";
import type { CallResult, SpeakdEvent } from "../speakd";

/** The source the export's verbs are sent as: the `zotero` owner, nobody's channel. */
export const EXPORT_SOURCE = "zotero:export";

/** A rate is only trusted after this much audio has been seen rendering. */
const RATE_MIN_AUDIO_SECONDS = 5;

/** What the exporter needs of Zotero and the daemon. */
export interface ExporterHost {
  call(verb: string, sourceId: string, payload?: Record<string, unknown>): Promise<CallResult>;
  /** The ledger's stored value, as it was last saved (or anything, if never). */
  loadLedger(): unknown;
  saveLedger(raw: string): void;
  /** The directory attach exports are rendered into, created if need be. */
  stagingDir(): Promise<string>;
  exists(path: string): Promise<boolean>;
  remove(path: string): Promise<void>;
  /** Imports `file` as the item's "Audio — <title>" attachment. */
  attach(tracked: TrackedJob, file: string): Promise<void>;
  tags(itemID: number): ItemTags;
  notify(title: string, message: string): void;
  /** Milliseconds, as Date.now(). */
  now(): number;
  warn(message: string, error?: unknown): void;
}

/** A job, as the item pane draws it. */
export interface JobEntry {
  readonly tracked: TrackedJob;
  readonly view: ProgressView;
}

function headline(title: string): string {
  return `Export audiobook: ${title}`;
}

function unreachable(result: Exclude<CallResult, { kind: "ok" }>): string {
  switch (result.kind) {
    case "no-daemon":
      return "speakd is not running.";
    case "bad-token":
      return "speakd refused the token; check the plugin's settings.";
    case "refused":
      return `speakd refused: ${result.error}`;
    case "http-error":
      return `speakd answered HTTP ${result.status}: ${result.error}`;
  }
}

export class Exporter {
  private readonly host: ExporterHost;
  private ledger: TrackedJob[];
  private readonly reports = new Map<string, JobReport>();
  /** The first running sample of each job: [wall ms, audio seconds]. */
  private readonly samples = new Map<string, [number, number]>();
  private readonly rates = new Map<string, number>();
  /** Jobs being attached or reported: done may be heard twice. */
  private readonly settling = new Set<string>();
  private readonly listeners = new Set<() => void>();
  /** The daemon's last measured real-time factor, from its `metrics` events. */
  rtf: number | undefined;

  constructor(host: ExporterHost) {
    this.host = host;
    this.ledger = parseLedger(host.loadLedger());
  }

  /** Hear any job change. Returns an unsubscribe. */
  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The jobs of an item, or of the PDF they were read from. */
  jobsFor(itemID: number): JobEntry[] {
    return this.ledger
      .filter((tracked) => tracked.itemID === itemID || tracked.attachmentID === itemID)
      .map((tracked) => ({
        tracked,
        view: describeProgress(tracked, this.reports.get(tracked.job) ?? null, this.rates.get(tracked.job) ?? null),
      }));
  }

  /** Render a confirmed export. Never rejects; what goes wrong is notified. */
  async start(request: ExportRequest): Promise<void> {
    const { choice } = request;
    try {
      const out =
        choice.destination === "folder" && choice.folder !== null
          ? await folderOut(choice.folder, request.title, choice.format, (path) => this.host.exists(path))
          : stagingOut(await this.host.stagingDir(), request.itemID, choice.format, this.host.now());
      const payload = renderPayload(request, out, this.host.tags(request.itemID));
      if (payload === null) {
        this.host.notify(headline(request.title), "The selection has no text.");
        return;
      }
      const result = await this.host.call("render", EXPORT_SOURCE, { ...payload });
      const job = result.kind === "ok" ? result.data.job : undefined;
      if (result.kind !== "ok" || typeof job !== "string") {
        this.host.notify(
          headline(request.title),
          result.kind === "ok" ? "speakd did not answer a job." : unreachable(result),
        );
        return;
      }
      this.track({
        job,
        itemID: request.itemID,
        attachmentID: request.attachmentID,
        title: request.title,
        format: choice.format,
        destination: choice.destination,
        out,
        expectedSeconds: expectedAudioSeconds(payload.parts),
      });
    } catch (error) {
      this.host.warn("starting the render failed", error);
      this.host.notify(headline(request.title), `Failed: ${String(error)}`);
    }
  }

  /** Stop a render. Its job is forgotten once speakd has heard. */
  async cancel(job: string): Promise<void> {
    const tracked = this.ledger.find((entry) => entry.job === job);
    if (tracked === undefined) return;
    const result = await this.host.call("render_cancel", EXPORT_SOURCE, { job });
    if (result.kind !== "ok") {
      this.host.notify(headline(tracked.title), `Could not cancel: ${unreachable(result)}`);
      return;
    }
    this.forget(job);
  }

  /** The daemon's event stream, as the link hears it. */
  onEvent(event: SpeakdEvent): void {
    if (event.event === "metrics") {
      if (typeof event.data.rtf === "number" && event.data.rtf > 0) this.rtf = event.data.rtf;
      return;
    }
    if (event.event !== "render") return;
    const report = parseJobReport(event.data);
    if (report === null) return;
    const tracked = this.ledger.find((entry) => entry.job === report.job);
    if (tracked === undefined) {
      // Perhaps one of ours whose `render` has not answered yet: the queued
      // event is published before the answer is sent.
      this.reports.set(report.job, report);
      return;
    }
    this.update(tracked, report);
  }

  /**
   * Match the ledger against `status`: on startup, and whenever the
   * connection comes back (events missed meanwhile are not replayed).
   */
  async sync(): Promise<void> {
    // Only the jobs known when status was asked: one started meanwhile is
    // missing from its answer without being lost.
    const asked = new Set(this.ledger.map((entry) => entry.job));
    const result = await this.host.call("status", EXPORT_SOURCE, {});
    if (result.kind !== "ok") return;
    const render = result.data.render;
    const raw = typeof render === "object" && render !== null ? (render as Record<string, unknown>).jobs : undefined;
    const reports = (Array.isArray(raw) ? raw : [])
      .map(parseJobReport)
      .filter((report): report is JobReport => report !== null);
    const known = this.ledger.filter((entry) => asked.has(entry.job));
    for (const pickup of pickUp(known, reports)) {
      switch (pickup.kind) {
        case "watch":
          this.update(pickup.tracked, pickup.report);
          break;
        case "finish":
          await this.finish(pickup.tracked, pickup.out);
          break;
        case "fail":
          this.fail(pickup.tracked, pickup.error);
          break;
        case "drop":
          this.forget(pickup.tracked.job);
          break;
        case "missing":
          // A daemon restarted after the render finished no longer lists
          // it; the file it wrote is the proof.
          if (await this.host.exists(pickup.tracked.out)) {
            await this.finish(pickup.tracked, pickup.tracked.out);
          } else {
            this.forget(pickup.tracked.job);
            this.host.notify(headline(pickup.tracked.title), "speakd no longer has this render.");
          }
          break;
      }
    }
  }

  private track(tracked: TrackedJob): void {
    this.ledger = [...this.ledger, tracked];
    this.save();
    const early = this.reports.get(tracked.job);
    if (early !== undefined) this.update(tracked, early);
    else this.changed();
  }

  private update(tracked: TrackedJob, report: JobReport): void {
    this.reports.set(report.job, report);
    if (report.state === "running") this.measure(report);
    switch (report.state) {
      case "done":
        void this.finish(tracked, report.out ?? tracked.out);
        return;
      case "failed":
        this.fail(tracked, report.error ?? "the render failed");
        return;
      case "cancelled":
        this.forget(tracked.job);
        return;
      default:
        this.changed();
    }
  }

  /** This render's own wall-clock cost per second of audio, once there is enough to tell. */
  private measure(report: JobReport): void {
    const now = this.host.now();
    const first = this.samples.get(report.job);
    if (first === undefined) {
      this.samples.set(report.job, [now, report.doneSeconds]);
      return;
    }
    const audio = report.doneSeconds - first[1];
    if (audio >= RATE_MIN_AUDIO_SECONDS) this.rates.set(report.job, (now - first[0]) / 1000 / audio);
  }

  private async finish(tracked: TrackedJob, out: string): Promise<void> {
    if (this.settling.has(tracked.job)) return;
    this.settling.add(tracked.job);
    try {
      if (tracked.destination === "folder") {
        this.host.notify(headline(tracked.title), `Saved to ${out}.`);
        return;
      }
      try {
        await this.host.attach(tracked, out);
      } catch (error) {
        this.host.warn("attaching the audio failed", error);
        this.host.notify(headline(tracked.title), `Could not attach the audio (${String(error)}); it is at ${out}.`);
        return;
      }
      // The staging file the plugin chose, never a path speakd reports.
      await this.host.remove(tracked.out).catch((error) => this.host.warn("removing the temporary audio failed", error));
      this.host.notify(headline(tracked.title), `Attached as “Audio — ${tracked.title}”.`);
    } finally {
      this.forget(tracked.job);
    }
  }

  private fail(tracked: TrackedJob, error: string): void {
    this.forget(tracked.job);
    this.host.notify(headline(tracked.title), `Failed: ${error}`);
  }

  private forget(job: string): void {
    this.reports.delete(job);
    this.samples.delete(job);
    this.rates.delete(job);
    const kept = this.ledger.filter((entry) => entry.job !== job);
    if (kept.length === this.ledger.length) return;
    this.ledger = kept;
    this.save();
    this.changed();
  }

  private save(): void {
    this.host.saveLedger(serializeLedger(this.ledger));
  }

  private changed(): void {
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch (error) {
        this.host.warn("an export listener failed", error);
      }
    }
  }
}
