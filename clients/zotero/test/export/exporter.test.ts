import { describe, expect, it } from "vitest";
import type { ExportChoice } from "../../src/export/dialog";
import type { ExportRequest } from "../../src/export/entry";
import { Exporter, EXPORT_SOURCE, type ExporterHost } from "../../src/export/exporter";
import { serializeLedger, type TrackedJob } from "../../src/export/jobs";
import type { CallResult } from "../../src/speakd";

const STAGING = "/home/u/profile/speakd-exports";

const text = (value: string, pageIndex = 0) => ({ text: value, pageIndex, anchor: null });

function request(choice: Partial<ExportChoice> = {}): ExportRequest {
  return {
    itemID: 3,
    title: "A Paper",
    attachmentID: 7,
    segments: [text("x".repeat(150))],
    choice: { selection: { kind: "whole" }, format: "mp3", destination: "attach", folder: null, ...choice },
  };
}

const TRACKED: TrackedJob = {
  job: "j1",
  itemID: 3,
  attachmentID: 7,
  title: "A Paper",
  format: "mp3",
  destination: "attach",
  out: `${STAGING}/3-1000.mp3`,
  expectedSeconds: 10,
};

const job = (state: string, change: Record<string, unknown> = {}) => ({
  job: "j1",
  state,
  part: 0,
  parts: 1,
  done_seconds: 0,
  estimate_seconds: 0,
  out: null,
  error: null,
  ...change,
});

function setup(
  options: { ledger?: TrackedJob[]; answers?: Record<string, CallResult>; files?: string[]; attachFails?: boolean } = {},
) {
  const calls: [string, string, Record<string, unknown>][] = [];
  const notes: [string, string][] = [];
  const attached: [number, string, string][] = [];
  const removed: string[] = [];
  const files = new Set(options.files ?? []);
  let ledger = options.ledger === undefined ? undefined : serializeLedger(options.ledger);
  let now = 1000;
  const answers: Record<string, CallResult> = {
    render: { kind: "ok", data: { job: "j1" } },
    render_cancel: { kind: "ok", data: { cancelled: true } },
    status: { kind: "ok", data: { render: { available: true, jobs: [] } } },
    ...options.answers,
  };
  const host: ExporterHost = {
    call: async (verb, sourceId, payload = {}) => {
      calls.push([verb, sourceId, payload]);
      return answers[verb]!;
    },
    loadLedger: () => ledger,
    saveLedger: (raw) => {
      ledger = raw;
    },
    stagingDir: async () => STAGING,
    exists: async (path) => files.has(path),
    remove: async (path) => {
      removed.push(path);
      files.delete(path);
    },
    attach: async (tracked, file) => {
      if (options.attachFails) throw new Error("disk full");
      attached.push([tracked.itemID, tracked.title, file]);
    },
    tags: () => ({ artist: "Ada" }),
    notify: (title, message) => void notes.push([title, message]),
    now: () => now,
    warn: () => {},
  };
  const exporter = new Exporter(host);
  return {
    exporter,
    calls,
    notes,
    attached,
    removed,
    files,
    ledger: () => ledger,
    answers,
    advance: (ms: number) => {
      now += ms;
    },
    event: (data: Record<string, unknown>) => exporter.onEvent({ event: "render", source_id: "", data }),
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("Exporter.start", () => {
  it("sends render with the pdf profile to a temporary file, and remembers the job", async () => {
    const run = setup();
    await run.exporter.start(request());
    expect(run.calls).toEqual([
      [
        "render",
        EXPORT_SOURCE,
        {
          parts: [{ title: "A Paper", text: "x".repeat(150) }],
          out: `${STAGING}/3-1000.mp3`,
          format: "mp3",
          profile: "pdf",
          metadata: { title: "A Paper", album: "A Paper", artist: "Ada" },
        },
      ],
    ]);
    expect(JSON.parse(run.ledger()!)).toEqual([TRACKED]);
    expect(run.exporter.jobsFor(3).map((entry) => entry.view.headline)).toEqual(["Waiting for speakd"]);
    expect(run.exporter.jobsFor(7)).toHaveLength(1);
    expect(run.exporter.jobsFor(4)).toEqual([]);
  });

  it("writes a folder export into the folder, under the title", async () => {
    const run = setup({ files: ["/home/u/Audio/A Paper.mp3"] });
    await run.exporter.start(request({ destination: "folder", folder: "/home/u/Audio" }));
    expect(run.calls[0]![2].out).toBe("/home/u/Audio/A Paper (2).mp3");
  });

  it("renders nothing for a selection without text, and says so", async () => {
    const run = setup();
    await run.exporter.start(request({ selection: { kind: "pages", ranges: [{ startPage: 4, endPage: 4 }] } }));
    expect(run.calls).toEqual([]);
    expect(run.notes).toEqual([["Export audiobook: A Paper", "The selection has no text."]]);
  });

  it("shows the daemon's refusal and remembers nothing", async () => {
    const run = setup({ answers: { render: { kind: "refused", error: "render: 'out' must be inside the user's home directory", data: {} } } });
    await run.exporter.start(request());
    expect(run.notes).toEqual([
      ["Export audiobook: A Paper", "speakd refused: render: 'out' must be inside the user's home directory"],
    ]);
    expect(run.exporter.jobsFor(3)).toEqual([]);
  });

  it("says when speakd is not there", async () => {
    const run = setup({ answers: { render: { kind: "no-daemon", error: "refused" } } });
    await run.exporter.start(request());
    expect(run.notes[0]![1]).toBe("speakd is not running.");
  });
});

describe("Exporter progress", () => {
  it("follows the render events of its own jobs only", async () => {
    const run = setup();
    await run.exporter.start(request());
    let changes = 0;
    run.exporter.onChange(() => changes++);
    run.event(job("running", { done_seconds: 5 }));
    run.event({ ...job("running"), job: "someone-else" });
    expect(run.exporter.jobsFor(3)[0]!.view).toMatchObject({ headline: "Rendering", percent: 50 });
    expect(changes).toBe(1);
  });

  it("keeps an event that came before render answered", async () => {
    const run = setup();
    run.event(job("running", { done_seconds: 2 }));
    await run.exporter.start(request());
    expect(run.exporter.jobsFor(3)[0]!.view.percent).toBe(20);
  });

  it("measures the render's own rate for the time left", async () => {
    const run = setup();
    await run.exporter.start(request());
    run.event(job("running", { done_seconds: 1, estimate_seconds: 100 }));
    run.advance(20_000);
    run.event(job("running", { done_seconds: 11, estimate_seconds: 100 }));
    // 20 s of wall clock for 10 s of audio: 89 s of audio left take 178 s.
    expect(run.exporter.jobsFor(3)[0]!.view.detail).toBe("about 3 min left");
  });

  it("keeps the daemon's measured real-time factor for the dialog's estimate", () => {
    const run = setup();
    expect(run.exporter.rtf).toBeUndefined();
    run.exporter.onEvent({ event: "metrics", source_id: "", data: { rtf: 0.4, queue: 0 } });
    expect(run.exporter.rtf).toBe(0.4);
  });
});

describe("Exporter on completion", () => {
  it("attaches the finished file, deletes the temporary copy, and says so", async () => {
    const run = setup();
    await run.exporter.start(request());
    run.files.add(`${STAGING}/3-1000.mp3`);
    run.event(job("done", { out: `${STAGING}/3-1000.mp3` }));
    await settle();
    expect(run.attached).toEqual([[3, "A Paper", `${STAGING}/3-1000.mp3`]]);
    expect(run.removed).toEqual([`${STAGING}/3-1000.mp3`]);
    expect(run.notes).toEqual([["Export audiobook: A Paper", "Attached as “Audio — A Paper”."]]);
    expect(run.exporter.jobsFor(3)).toEqual([]);
    expect(JSON.parse(run.ledger()!)).toEqual([]);
  });

  it("attaches once, however often done is heard", async () => {
    const run = setup({ answers: { status: { kind: "ok", data: { render: { jobs: [job("done")] } } } } });
    await run.exporter.start(request());
    run.event(job("done"));
    await run.exporter.sync();
    run.event(job("done"));
    await settle();
    expect(run.attached).toHaveLength(1);
  });

  it("leaves a folder export where it is", async () => {
    const run = setup();
    await run.exporter.start(request({ destination: "folder", folder: "/home/u/Audio" }));
    run.event(job("done", { out: "/home/u/Audio/A Paper.mp3" }));
    await settle();
    expect(run.attached).toEqual([]);
    expect(run.removed).toEqual([]);
    expect(run.notes).toEqual([["Export audiobook: A Paper", "Saved to /home/u/Audio/A Paper.mp3."]]);
  });

  it("keeps the file and says where when attaching fails", async () => {
    const run = setup({ attachFails: true });
    await run.exporter.start(request());
    run.event(job("done"));
    await settle();
    expect(run.removed).toEqual([]);
    expect(run.notes[0]![1]).toBe(`Could not attach the audio (Error: disk full); it is at ${STAGING}/3-1000.mp3.`);
    expect(run.exporter.jobsFor(3)).toEqual([]);
  });

  it("says a render failed, with the daemon's error", async () => {
    const run = setup();
    await run.exporter.start(request());
    run.event(job("failed", { error: "ffmpeg exited 1" }));
    await settle();
    expect(run.notes).toEqual([["Export audiobook: A Paper", "Failed: ffmpeg exited 1"]]);
    expect(run.exporter.jobsFor(3)).toEqual([]);
  });
});

describe("Exporter.cancel", () => {
  it("sends render_cancel and forgets the job", async () => {
    const run = setup();
    await run.exporter.start(request());
    await run.exporter.cancel("j1");
    expect(run.calls.at(-1)).toEqual(["render_cancel", EXPORT_SOURCE, { job: "j1" }]);
    expect(run.exporter.jobsFor(3)).toEqual([]);
    expect(JSON.parse(run.ledger()!)).toEqual([]);
  });

  it("keeps the job when speakd could not be reached", async () => {
    const run = setup({ answers: { render_cancel: { kind: "no-daemon", error: "x" } } });
    await run.exporter.start(request());
    await run.exporter.cancel("j1");
    expect(run.exporter.jobsFor(3)).toHaveLength(1);
    expect(run.notes[0]![1]).toBe("Could not cancel: speakd is not running.");
  });
});

describe("Exporter.sync, on start", () => {
  it("shows the progress of a render that went on while Zotero was closed", async () => {
    const run = setup({
      ledger: [TRACKED],
      answers: { status: { kind: "ok", data: { render: { jobs: [job("running", { done_seconds: 5 })] } } } },
    });
    expect(run.exporter.jobsFor(3)[0]!.view.headline).toBe("Waiting for speakd");
    await run.exporter.sync();
    expect(run.exporter.jobsFor(3)[0]!.view).toMatchObject({ headline: "Rendering", percent: 50 });
  });

  it("attaches a render that finished while Zotero was closed", async () => {
    const run = setup({
      ledger: [TRACKED],
      answers: { status: { kind: "ok", data: { render: { jobs: [job("done", { out: TRACKED.out })] } } } },
    });
    await run.exporter.sync();
    expect(run.attached).toEqual([[3, "A Paper", TRACKED.out]]);
  });

  it("attaches a finished file speakd has since forgotten", async () => {
    const run = setup({ ledger: [TRACKED], files: [TRACKED.out] });
    await run.exporter.sync();
    expect(run.attached).toEqual([[3, "A Paper", TRACKED.out]]);
  });

  it("forgets, and says so, a render speakd lost", async () => {
    const run = setup({ ledger: [TRACKED] });
    await run.exporter.sync();
    expect(run.attached).toEqual([]);
    expect(run.notes).toEqual([["Export audiobook: A Paper", "speakd no longer has this render."]]);
    expect(run.exporter.jobsFor(3)).toEqual([]);
  });

  it("keeps every job while speakd cannot be asked", async () => {
    const run = setup({ ledger: [TRACKED], answers: { status: { kind: "no-daemon", error: "x" } } });
    await run.exporter.sync();
    expect(run.exporter.jobsFor(3)).toHaveLength(1);
    expect(run.notes).toEqual([]);
  });
});
