import { describe, expect, it } from "vitest";
import type { ExportChoice } from "../../src/export/dialog";
import type { ExportRequest } from "../../src/export/entry";
import { expectedAudioSeconds, fileStem, folderOut, renderPayload, stagingOut } from "../../src/export/render";

const text = (value: string, pageIndex = 0, anchor: string | null = null) => ({ text: value, pageIndex, anchor });

function request(choice: Partial<ExportChoice> = {}, segments = [text("One."), text("Two.", 1)]): ExportRequest {
  return {
    itemID: 3,
    title: "A Paper",
    attachmentID: 7,
    segments,
    choice: { selection: { kind: "whole" }, format: "mp3", destination: "attach", folder: null, ...choice },
  };
}

describe("renderPayload", () => {
  it("sends the parts with the pdf profile, the format and the item's tags", () => {
    expect(renderPayload(request(), "/home/u/x.mp3", { artist: "Ada Lovelace", date: "1843" })).toEqual({
      parts: [{ title: "A Paper", text: "One. Two." }],
      out: "/home/u/x.mp3",
      format: "mp3",
      profile: "pdf",
      metadata: { title: "A Paper", album: "A Paper", artist: "Ada Lovelace", date: "1843" },
    });
  });

  it("leaves out tags the item does not have", () => {
    expect(renderPayload(request(), "/home/u/x.mp3", {})?.metadata).toEqual({ title: "A Paper", album: "A Paper" });
  });

  it("makes one part per chapter", () => {
    const payload = renderPayload(
      request({
        format: "m4b",
        selection: {
          kind: "chapters",
          chapters: [
            { title: "One", startPage: 0, endPage: 0 },
            { title: "Two", startPage: 1, endPage: 1 },
          ],
        },
      }),
      "/home/u/x.m4b",
      {},
    );
    expect(payload?.parts).toEqual([
      { title: "One", text: "One." },
      { title: "Two", text: "Two." },
    ]);
    expect(payload?.format).toBe("m4b");
  });

  it("is null when the selection has no text, so nothing is rendered", () => {
    expect(renderPayload(request({ selection: { kind: "pages", ranges: [{ startPage: 5, endPage: 6 }] } }), "/x.mp3", {})).toBeNull();
  });
});

describe("expectedAudioSeconds", () => {
  it("counts the parts' characters at the default speaking rate", () => {
    expect(expectedAudioSeconds([{ title: "a", text: "x".repeat(150) }, { title: "b", text: "y".repeat(150) }])).toBe(20);
  });
});

describe("fileStem", () => {
  it("keeps an ordinary title", () => {
    expect(fileStem("Being and Time")).toBe("Being and Time");
  });

  it("replaces what a file name cannot hold", () => {
    expect(fileStem('A/B: "C"? <D>|E*\\F')).toBe("A-B- -C-- -D--E--F");
  });

  it("drops leading dots and collapses whitespace", () => {
    expect(fileStem("..  hidden\n title  ")).toBe("hidden title");
  });

  it("caps the length", () => {
    expect(fileStem("x".repeat(300))).toHaveLength(120);
  });

  it("falls back to a name for an empty title", () => {
    expect(fileStem(" / ")).toBe("-");
    expect(fileStem("   ")).toBe("audiobook");
  });
});

describe("stagingOut", () => {
  it("names the temporary file by item and time, with the format's extension", () => {
    expect(stagingOut("/home/u/.zotero/p/speakd-exports", 3, "m4b", 1700)).toBe(
      "/home/u/.zotero/p/speakd-exports/3-1700.m4b",
    );
  });
});

describe("folderOut", () => {
  it("names the file after the title in the folder", async () => {
    expect(await folderOut("/home/u/Audio/", "A Paper", "opus", () => false)).toBe("/home/u/Audio/A Paper.opus");
  });

  it("never overwrites a file that is already there", async () => {
    const taken = new Set(["/home/u/Audio/A Paper.mp3", "/home/u/Audio/A Paper (2).mp3"]);
    expect(await folderOut("/home/u/Audio", "A Paper", "mp3", (path) => taken.has(path))).toBe("/home/u/Audio/A Paper (3).mp3");
  });
});
