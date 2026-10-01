import { describe, expect, it } from "vitest";
import type { CaptureHandle, CaptureHost } from "../../src/export/capture";
import type { ExportChoice } from "../../src/export/dialog";
import { exportFlow, type ExportFlowDeps, type ExportRequest } from "../../src/export/entry";
import type { CaptureResult } from "../../src/session";

const TARGET = { attachmentID: 7, itemID: 3 };
const CHOICE: ExportChoice = {
  selection: { kind: "whole", skipReferences: true },
  format: "mp3",
  destination: "attach",
  folder: null,
};

interface Reader {
  name: string;
}

function setup(options: { capture: CaptureResult; choice?: ExportChoice | null; open?: Reader; structureFails?: boolean }) {
  const log: string[] = [];
  const problems: [string, string][] = [];
  const requests: ExportRequest[] = [];
  const dialogs: unknown[] = [];
  const handle: CaptureHandle = {
    adoption: { kind: "ready" },
    capture: async () => options.capture,
    onChange: () => () => {},
  };
  let opened = 0;
  const host: CaptureHost<Reader> = {
    find: () => options.open ?? null,
    open: async () => ({ name: `background${++opened}` }),
    close: (reader) => log.push(`close ${reader.name}`),
    handleFor: () => handle,
    later: () => () => {},
  };
  const deps: ExportFlowDeps = {
    title: "A Paper",
    itemType: "journalArticle",
    captureHost: host,
    structure: async () => {
      if (options.structureFails) throw new Error("no outline");
      return { pageCount: 4, outline: null };
    },
    openDialog: async (request) => {
      dialogs.push(request);
      return options.choice === undefined ? CHOICE : options.choice;
    },
    onRequest: (request) => void requests.push(request),
    onProblem: (title, message) => void problems.push([title, message]),
    warn: () => {},
  };
  return { deps, log, problems, requests, dialogs, opened: () => opened };
}

const text = (value: string, pageIndex = 0) => ({ text: value, pageIndex, anchor: null });

describe("exportFlow", () => {
  it("shows the no-text problem for a capture with no text, opening no dialog and making no request", async () => {
    const run = setup({ capture: { ok: true, segments: [text(""), text("  ")] } });
    await exportFlow(TARGET, run.deps);
    expect(run.problems).toEqual([["A Paper", "this document has no text to read"]]);
    expect(run.dialogs).toEqual([]);
    expect(run.requests).toEqual([]);
  });

  it("shows no-text for an empty segment list too", async () => {
    const run = setup({ capture: { ok: true, segments: [] } });
    await exportFlow(TARGET, run.deps);
    expect(run.problems).toHaveLength(1);
    expect(run.dialogs).toEqual([]);
    expect(run.requests).toEqual([]);
  });

  it("shows a capture problem, opening no dialog and making no request", async () => {
    const run = setup({ capture: { ok: false, problem: { kind: "zotero", error: "the reader is closed" } } });
    await exportFlow(TARGET, run.deps);
    expect(run.problems).toEqual([["A Paper", "the reader is closed"]]);
    expect(run.dialogs).toEqual([]);
    expect(run.requests).toEqual([]);
  });

  it("opens the dialog and passes the confirmed choice on as a request", async () => {
    const segments = [text("Hello there.", 0), text("More text.", 3)];
    const run = setup({ capture: { ok: true, segments } });
    await exportFlow(TARGET, run.deps);
    expect(run.problems).toEqual([]);
    expect(run.dialogs).toHaveLength(1);
    expect(run.dialogs[0]).toMatchObject({ item: { itemType: "journalArticle", title: "A Paper", pageCount: 4 }, segments });
    expect(run.requests).toEqual([{ itemID: 3, title: "A Paper", attachmentID: 7, segments, choice: CHOICE }]);
  });

  it("makes no request when the dialog is cancelled", async () => {
    const run = setup({ capture: { ok: true, segments: [text("Hello.")] }, choice: null });
    await exportFlow(TARGET, run.deps);
    expect(run.dialogs).toHaveLength(1);
    expect(run.requests).toEqual([]);
    expect(run.problems).toEqual([]);
  });

  it("falls back to pages only when the structure cannot be read", async () => {
    const run = setup({ capture: { ok: true, segments: [text("a", 0), text("b", 2)] }, structureFails: true });
    await exportFlow(TARGET, run.deps);
    expect(run.dialogs[0]).toMatchObject({ item: { chapters: null, pageCount: 3 } });
    expect(run.requests).toHaveLength(1);
  });

  it("closes every background reader it opened, capture's and structure's", async () => {
    const run = setup({ capture: { ok: true, segments: [text("Hello.")] } });
    await exportFlow(TARGET, run.deps);
    expect(run.opened()).toBe(2);
    expect(run.log.sort()).toEqual(["close background1", "close background2"]);
  });

  it("closes the reader even when the structure read throws", async () => {
    const run = setup({ capture: { ok: true, segments: [text("Hello.")] }, structureFails: true });
    await exportFlow(TARGET, run.deps);
    expect(run.log.sort()).toEqual(["close background1", "close background2"]);
  });

  it("opens nothing, and closes nothing, for a reader the user already has", async () => {
    const run = setup({ capture: { ok: true, segments: [text("Hello.")] }, open: { name: "users" } });
    await exportFlow(TARGET, run.deps);
    expect(run.opened()).toBe(0);
    expect(run.log).toEqual([]);
  });

  it("turns a throwing dialog into a problem rather than a rejection", async () => {
    const run = setup({ capture: { ok: true, segments: [text("Hello.")] } });
    run.deps.openDialog = async () => {
      throw new Error("boom");
    };
    await exportFlow(TARGET, run.deps);
    expect(run.problems).toEqual([["Export audiobook", "Error: boom"]]);
    expect(run.requests).toEqual([]);
  });
});
