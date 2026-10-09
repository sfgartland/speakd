// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";
import { Labeler, KEY_CATEGORIES, mountCapturePane, predictCategories, segmentsOnPage } from "../src/labeler";
import { locatorFor, type LabelFile, type LabelRecord } from "../src/labelstore";
import type { CapturedSegment } from "../src/session";

function segment(
  text: string,
  pageIndex = 0,
  anchor: string | null = "paragraphStart",
  firstRectY: number | null = 740,
): CapturedSegment {
  return { text, pageIndex, anchor, rectCount: firstRectY === null ? null : 1, firstRectY };
}

function emptyFile(): LabelFile {
  return {
    schema: 1,
    attachmentKey: "ATTACH01",
    documentSha256: "doc",
    capture: { zoteroVersion: "10.0.6", segments: 0, labelledAt: 0 },
    labels: [],
  };
}

function setup(options: { segments?: CapturedSegment[]; file?: LabelFile; unmatched?: LabelRecord[] } = {}) {
  const save = vi.fn<(file: LabelFile) => Promise<void>>(async () => {});
  const onSelect = vi.fn();
  const onExit = vi.fn();
  const labeler = new Labeler({
    document,
    segments: options.segments ?? [segment("Body one."), segment("Body two.")],
    policy: "classify",
    file: options.file ?? emptyFile(),
    unmatched: options.unmatched ?? [],
    save,
    now: () => 1760000000,
    onSelect,
    onExit,
    warn: () => {},
  });
  labeler.mount();
  return { labeler, save, onSelect, onExit };
}

const chips = (root: HTMLElement) => [...root.querySelectorAll<HTMLElement>(".speakd-label-chip")];
const itemFor = (root: HTMLElement, index: number) => root.querySelector<HTMLElement>(`.speakd-label-item[data-index="${index}"]`);

describe("predictCategories", () => {
  it("seeds body for body, footnote for a detected candidate, and running-head for a dropped head", () => {
    const segments = [
      segment("A plain body paragraph long enough to be body."),
      segment("1 Kant, KrV, B25.", 0, "paragraphStart", 140),
      segment("2 A prose footnote that holds two sentences. It carries on for a while.", 0, "paragraphStart", 130),
    ];
    expect(predictCategories(segments, "classify")).toEqual(["body", "footnote", "footnote"]);
  });

  it("seeds running-head for a segment the recurrence filter drops", () => {
    const segments = [];
    for (let n = 0; n < 5; n++) {
      segments.push(segment("Sample Journal", n, "paragraphStart", 770));
      segments.push(segment(`A body paragraph of page ${n} long enough to survive the recurrence filter, with plenty of words to spare.`, n, "paragraphStart", 740));
    }
    const predicted = predictCategories(segments, "classify");
    expect(predicted[0]).toBe("running-head");
    expect(predicted[1]).toBe("body");
  });
});

describe("segmentsOnPage", () => {
  it("returns the indices on one page, in order", () => {
    const segments = [segment("a", 0), segment("b", 1), segment("c", 0), segment("d", 1)];
    expect(segmentsOnPage(segments, 0)).toEqual([0, 2]);
    expect(segmentsOnPage(segments, 1)).toEqual([1, 3]);
  });
});

describe("the label pane", () => {
  it("lists the current page's segments with a chip seeded from the prediction", () => {
    const segments = [segment("Body one."), segment("1 Kant, KrV, B25.", 0, "paragraphStart", 140)];
    const { labeler } = setup({ segments });
    expect(chips(labeler.root)).toHaveLength(2);
    expect(itemFor(labeler.root, 0)?.querySelector(".speakd-label-chip")?.getAttribute("data-category")).toBe("body");
    expect(itemFor(labeler.root, 1)?.querySelector(".speakd-label-chip")?.getAttribute("data-category")).toBe("footnote");
  });

  it("shows only the current page and follows the selection into the next page", () => {
    const segments = [segment("Page zero.", 0), segment("Page one.", 1)];
    const { labeler } = setup({ segments });
    expect(chips(labeler.root)).toHaveLength(1);
    labeler.select(1);
    expect(labeler.page).toBe(1);
    expect(chips(labeler.root)).toHaveLength(1);
    expect(itemFor(labeler.root, 1)).not.toBeNull();
    expect(itemFor(labeler.root, 0)).toBeNull();
  });

  it("writes a LabelRecord through the store on a category key and updates the chip", async () => {
    const segments = [segment("Body one."), segment("1 Kant, KrV, B25.", 0, "paragraphStart", 140)];
    const { labeler, save } = setup({ segments });
    labeler.select(1);
    labeler.keydown(new KeyboardEvent("keydown", { key: "b", bubbles: true }));
    await Promise.resolve();
    expect(save).toHaveBeenCalledTimes(1);
    const written = save.mock.calls[0]![0];
    const record = written.labels.find((entry) => entry.category === "body");
    expect(record).toBeDefined();
    expect(record?.locator.textSha256).toBe(locatorFor(segments[1]!).textSha256);
    expect(itemFor(labeler.root, 1)?.querySelector(".speakd-label-chip")?.getAttribute("data-category")).toBe("body");
  });

  it("keeps its keys pane-scoped: a listener on the document never sees them", () => {
    const { labeler } = setup();
    const seen: string[] = [];
    document.addEventListener("keydown", (event) => seen.push((event as KeyboardEvent).key));
    labeler.root.dispatchEvent(new KeyboardEvent("keydown", { key: "f", bubbles: true }));
    expect(seen).toEqual([]);
    // A key on the document body never reaches the pane.
    const before = labeler.overrides.size;
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "k", bubbles: true }));
    expect(labeler.overrides.size).toBe(before);
  });

  it("exits on Escape, stopping the event from reaching the reader", () => {
    const { labeler, onExit } = setup();
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    labeler.root.dispatchEvent(event);
    expect(onExit).toHaveBeenCalledTimes(1);
    expect(event.cancelBubble).toBe(true);
  });

  it("lists unmatched labels and jumps to the next unlabeled segment", async () => {
    const unmatched: LabelRecord[] = [
      {
        locator: { page: 9, rectY: null, anchor: null, prefix: "gone", textSha256: "x" },
        category: "junk",
        labelledAt: 0,
        note: "",
      },
    ];
    const segments = [segment("Body one."), segment("Body two.")];
    const { labeler, save } = setup({ segments, unmatched });
    expect(labeler.root.textContent).toContain("1 label no longer matches");
    labeler.select(0);
    labeler.setCategory(0, "body");
    await Promise.resolve();
    expect(save).toHaveBeenCalled();
    labeler.nextUnlabeled();
    expect(labeler.selected).toBe(1);
  });

  it("calls onSelect when a segment is selected", () => {
    const { labeler, onSelect } = setup();
    labeler.select(1);
    expect(onSelect).toHaveBeenCalledWith(1);
  });
});

describe("the capturing pane", () => {
  it("shows elapsed seconds, never a percentage, and aborts on Escape", () => {
    const onAbort = vi.fn();
    const pane = mountCapturePane(document, onAbort);
    pane.setElapsed(7);
    expect(pane.root.textContent).toContain("capturing");
    expect(pane.root.textContent).toContain("7");
    expect(pane.root.textContent).not.toContain("%");
    pane.root.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(onAbort).toHaveBeenCalledTimes(1);
    pane.dispose();
  });

  it("says why a capture failed", () => {
    const pane = mountCapturePane(document, () => {});
    pane.fail("the PDF took too long to prepare for reading");
    expect(pane.root.textContent).toContain("the PDF took too long to prepare for reading");
    pane.dispose();
  });
});

describe("category keys", () => {
  it("maps every spec key to a category", () => {
    expect(KEY_CATEGORIES).toEqual({
      b: "body",
      f: "footnote",
      h: "running-head",
      p: "page-number",
      k: "junk",
      g: "heading",
    });
  });
});
