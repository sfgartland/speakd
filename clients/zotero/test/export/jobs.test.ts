import { describe, expect, it } from "vitest";
import {
  describeProgress,
  parseJobReport,
  parseLedger,
  pickUp,
  serializeLedger,
  type JobReport,
  type TrackedJob,
} from "../../src/export/jobs";

const TRACKED: TrackedJob = {
  job: "abc",
  itemID: 3,
  attachmentID: 7,
  title: "A Paper",
  format: "mp3",
  destination: "attach",
  out: "/home/u/p/speakd-exports/3-1.mp3",
  expectedSeconds: 600,
};

function report(change: Partial<JobReport> = {}): JobReport {
  return {
    job: "abc",
    state: "running",
    part: 0,
    parts: 1,
    doneSeconds: 0,
    estimateSeconds: 0,
    out: null,
    error: null,
    ...change,
  };
}

describe("parseJobReport", () => {
  it("reads the daemon's job dict", () => {
    expect(
      parseJobReport({
        job: "abc",
        state: "done",
        part: 2,
        parts: 2,
        done_seconds: 12.5,
        estimate_seconds: 0,
        out: "/home/u/x.mp3",
        error: null,
      }),
    ).toEqual(report({ state: "done", part: 2, parts: 2, doneSeconds: 12.5, out: "/home/u/x.mp3" }));
  });

  it("refuses anything without a job id or a known state", () => {
    expect(parseJobReport({ state: "running" })).toBeNull();
    expect(parseJobReport({ job: "abc", state: "exploded" })).toBeNull();
    expect(parseJobReport(null)).toBeNull();
  });

  it("takes missing numbers as zero", () => {
    expect(parseJobReport({ job: "abc", state: "queued" })).toEqual(report({ state: "queued", parts: 0 }));
  });
});

describe("the ledger", () => {
  it("round-trips", () => {
    expect(parseLedger(serializeLedger([TRACKED]))).toEqual([TRACKED]);
  });

  it("is empty for anything unreadable, and skips malformed entries", () => {
    expect(parseLedger(undefined)).toEqual([]);
    expect(parseLedger("not json")).toEqual([]);
    expect(parseLedger('{"job":"x"}')).toEqual([]);
    expect(parseLedger(JSON.stringify([TRACKED, { job: "y" }, { ...TRACKED, format: "wav" }]))).toEqual([TRACKED]);
  });
});

describe("pickUp", () => {
  it("watches what is still queued, running or paused", () => {
    for (const state of ["queued", "running", "paused"] as const) {
      expect(pickUp([TRACKED], [report({ state })])).toEqual([{ kind: "watch", tracked: TRACKED, report: report({ state }) }]);
    }
  });

  it("finishes a render that completed while Zotero was closed, at the daemon's path", () => {
    expect(pickUp([TRACKED], [report({ state: "done", out: "/home/u/resolved.mp3" })])).toEqual([
      { kind: "finish", tracked: TRACKED, out: "/home/u/resolved.mp3" },
    ]);
  });

  it("finishes at the planned path when the daemon gives none", () => {
    expect(pickUp([TRACKED], [report({ state: "done" })])).toEqual([{ kind: "finish", tracked: TRACKED, out: TRACKED.out }]);
  });

  it("reports a failure with the daemon's error, and drops a cancelled render", () => {
    expect(pickUp([TRACKED], [report({ state: "failed", error: "ffmpeg died" })])).toEqual([
      { kind: "fail", tracked: TRACKED, error: "ffmpeg died" },
    ]);
    expect(pickUp([TRACKED], [report({ state: "cancelled" })])).toEqual([{ kind: "drop", tracked: TRACKED }]);
  });

  it("marks a render the daemon no longer knows as missing", () => {
    expect(pickUp([TRACKED], [report({ job: "other" })])).toEqual([{ kind: "missing", tracked: TRACKED }]);
  });
});

describe("describeProgress", () => {
  it("shows a queued render as waiting", () => {
    expect(describeProgress(TRACKED, report({ state: "queued" }), null)).toEqual({
      headline: "Waiting to render",
      percent: 0,
      detail: "",
    });
  });

  it("shows the share of the expected audio rendered, and the time left at the default rate", () => {
    expect(describeProgress(TRACKED, report({ doneSeconds: 150 }), null)).toEqual({
      headline: "Rendering",
      percent: 25,
      detail: "about 8 min left",
    });
  });

  it("uses the measured rate for the time left", () => {
    expect(describeProgress(TRACKED, report({ doneSeconds: 300 }), 0.1).detail).toBe("about 30 s left");
  });

  it("prefers the daemon's estimate of the audio, and names the part of several", () => {
    expect(describeProgress(TRACKED, report({ doneSeconds: 50, estimateSeconds: 100, part: 1, parts: 3 }), null)).toEqual({
      headline: "Rendering part 2 of 3",
      percent: 50,
      detail: "about 50 s left",
    });
  });

  it("never claims 100 % before the render is done", () => {
    expect(describeProgress(TRACKED, report({ doneSeconds: 900 }), null).percent).toBe(99);
    expect(describeProgress(TRACKED, report({ state: "done", doneSeconds: 900 }), null)).toEqual({
      headline: "Done",
      percent: 100,
      detail: "",
    });
  });

  it("explains a pause, a failure and a cancel", () => {
    expect(describeProgress(TRACKED, report({ state: "paused", doneSeconds: 60 }), null)).toMatchObject({
      headline: "Paused while speakd speaks",
      percent: 10,
    });
    expect(describeProgress(TRACKED, report({ state: "failed", error: "no ffmpeg" }), null)).toEqual({
      headline: "Failed",
      percent: null,
      detail: "no ffmpeg",
    });
    expect(describeProgress(TRACKED, report({ state: "cancelled" }), null).headline).toBe("Cancelled");
  });

  it("says when nothing has been heard from speakd yet", () => {
    expect(describeProgress(TRACKED, null, null)).toEqual({ headline: "Waiting for speakd", percent: null, detail: "" });
  });
});
