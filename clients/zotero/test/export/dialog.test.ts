import { describe, expect, it } from "vitest";
import {
  availableModes,
  describeState,
  initialState,
  dialogItem,
  isFrontBackMatter,
  parseSettings,
  resolveChoice,
  DEFAULT_SETTINGS,
  type DialogItem,
} from "../../src/export/dialog";

const chapters = [
  { title: "Contents", startPage: 0, endPage: 1 },
  { title: "One", startPage: 2, endPage: 9 },
  { title: "Two", startPage: 10, endPage: 19 },
  { title: "Index", startPage: 20, endPage: 24 },
];

const article: DialogItem = { itemType: "journalArticle", title: "A Paper", chapters: null, pageCount: 25 };
const book: DialogItem = { itemType: "book", title: "A Book", chapters, pageCount: 25 };
const bareBook: DialogItem = { itemType: "book", title: "A Book", chapters: null, pageCount: 25 };

function seg(text: string, pageIndex: number, anchor: string | null = null) {
  return { text, pageIndex, anchor };
}

describe("parseSettings", () => {
  it("falls back to the declared defaults for anything missing or invalid", () => {
    expect(parseSettings({})).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings({ formatArticle: "wav", destination: 3, skipReferences: "yes" })).toEqual(DEFAULT_SETTINGS);
  });

  it("declares mp3 for articles, m4b for books, attach, no folder, skip references", () => {
    expect(DEFAULT_SETTINGS).toEqual({
      formatArticle: "mp3",
      formatBook: "m4b",
      destination: "attach",
      folder: "",
      skipReferences: true,
    });
  });

  it("takes valid values", () => {
    expect(
      parseSettings({ formatArticle: "opus", formatBook: "mp3", destination: "folder", folder: " /x ", skipReferences: false }),
    ).toEqual({ formatArticle: "opus", formatBook: "mp3", destination: "folder", folder: "/x", skipReferences: false });
  });
});

describe("availableModes", () => {
  it("offers an article the whole text or pages", () => {
    expect(availableModes(article)).toEqual(["whole", "pages"]);
  });
  it("offers a book with an outline its chapters first", () => {
    expect(availableModes(book)).toEqual(["chapters", "pages", "whole"]);
  });
  it("offers a book without an outline a page range only", () => {
    expect(availableModes(bareBook)).toEqual(["pages"]);
    expect(availableModes({ ...bareBook, chapters: [] })).toEqual(["pages"]);
  });
  it("treats a book section like a book", () => {
    expect(availableModes({ ...bareBook, itemType: "bookSection" })).toEqual(["pages"]);
  });
});

describe("initialState", () => {
  it("defaults an article to the whole text, mp3, attached, references skipped", () => {
    expect(initialState(article, DEFAULT_SETTINGS)).toMatchObject({
      mode: "whole",
      format: "mp3",
      destination: "attach",
      skipReferences: true,
      folder: "",
    });
  });
  it("defaults a book to chapters and m4b", () => {
    const state = initialState(book, DEFAULT_SETTINGS);
    expect(state.mode).toBe("chapters");
    expect(state.format).toBe("m4b");
  });
  it("starts a book without an outline on an empty page range", () => {
    const state = initialState(bareBook, DEFAULT_SETTINGS);
    expect(state.mode).toBe("pages");
    expect(state.pages).toBe("");
  });
  it("unticks front and back matter chapters", () => {
    expect(initialState(book, DEFAULT_SETTINGS).chapterChecks).toEqual([false, true, true, false]);
  });
  it("follows the settings", () => {
    const state = initialState(article, { ...DEFAULT_SETTINGS, formatArticle: "opus", destination: "folder", folder: "/a", skipReferences: false });
    expect(state).toMatchObject({ format: "opus", destination: "folder", folder: "/a", skipReferences: false });
  });
});

describe("isFrontBackMatter", () => {
  it("recognises the usual suspects, case-insensitively", () => {
    for (const title of ["Contents", "table of contents", "Copyright", "Index", "Bibliography", "Acknowledgments", "About the Author"]) {
      expect(isFrontBackMatter(title)).toBe(true);
    }
  });
  it("leaves real chapters", () => {
    for (const title of ["Introduction", "Chapter 1: Contents of the Mind", "Notes on Method"]) {
      expect(isFrontBackMatter(title)).toBe(false);
    }
  });
});

describe("resolveChoice", () => {
  it("makes a whole selection carrying the references toggle", () => {
    const result = resolveChoice(initialState(article, DEFAULT_SETTINGS), article);
    expect(result).toEqual({
      ok: true,
      choice: {
        selection: { kind: "whole", skipReferences: true },
        format: "mp3",
        destination: "attach",
        folder: null,
      },
    });
  });

  it("makes a chapter selection from the ticked chapters only", () => {
    const result = resolveChoice(initialState(book, DEFAULT_SETTINGS), book);
    expect(result.ok && result.choice.selection).toEqual({ kind: "chapters", chapters: [chapters[1], chapters[2]] });
  });

  it("refuses no chapters ticked", () => {
    const state = { ...initialState(book, DEFAULT_SETTINGS), chapterChecks: [false, false, false, false] };
    expect(resolveChoice(state, book)).toEqual({ ok: false, error: "Tick at least one chapter." });
  });

  it("parses pages and refuses an empty or bad field", () => {
    const state = { ...initialState(bareBook, DEFAULT_SETTINGS), pages: "3-5, 9" };
    const result = resolveChoice(state, bareBook);
    expect(result.ok && result.choice.selection).toEqual({
      kind: "pages",
      ranges: [
        { startPage: 2, endPage: 4 },
        { startPage: 8, endPage: 8 },
      ],
    });
    expect(resolveChoice(initialState(bareBook, DEFAULT_SETTINGS), bareBook).ok).toBe(false);
    expect(resolveChoice({ ...state, pages: "x" }, bareBook).ok).toBe(false);
  });

  it("refuses pages past the end of the document", () => {
    const state = { ...initialState(bareBook, DEFAULT_SETTINGS), pages: "20-30" };
    const result = resolveChoice(state, bareBook);
    expect(result).toEqual({ ok: false, error: "The document has 25 pages." });
  });

  it("requires a folder when the destination is a folder, and carries it", () => {
    const state = { ...initialState(article, DEFAULT_SETTINGS), destination: "folder" as const };
    expect(resolveChoice(state, article)).toEqual({ ok: false, error: "Choose a folder." });
    const chosen = resolveChoice({ ...state, folder: "/audio" }, article);
    expect(chosen.ok && chosen.choice).toMatchObject({ destination: "folder", folder: "/audio" });
  });

  it("drops the folder when attaching", () => {
    const state = { ...initialState(article, DEFAULT_SETTINGS), folder: "/audio" };
    const result = resolveChoice(state, article);
    expect(result.ok && result.choice.folder).toBeNull();
  });
});

describe("describeState", () => {
  const segments = [seg("a".repeat(150), 0), seg("b".repeat(150), 1)];

  it("estimates from the selected text and the measured rtf", () => {
    const state = initialState(article, DEFAULT_SETTINGS);
    expect(describeState(segments, article, state, 2)).toEqual({ ok: true, estimate: "about 40 s" });
  });

  it("counts only the selected pages", () => {
    const state = { ...initialState(article, DEFAULT_SETTINGS), mode: "pages" as const, pages: "1" };
    expect(describeState(segments, article, state, 1)).toEqual({ ok: true, estimate: "about 10 s" });
  });

  it("reports a selection with no text instead of an estimate", () => {
    const state = { ...initialState(article, DEFAULT_SETTINGS), mode: "pages" as const, pages: "5" };
    expect(describeState(segments, article, state)).toEqual({ ok: false, error: "The selection has no text." });
  });

  it("passes on a selection error", () => {
    const state = initialState(bareBook, DEFAULT_SETTINGS);
    expect(describeState(segments, bareBook, state)).toMatchObject({ ok: false });
  });
});

describe("dialogItem", () => {
  it("turns an outline into chapters, dropping those that share a page with the next", () => {
    const item = dialogItem("book", "B", {
      pageCount: 10,
      outline: [
        { title: "Part I", pageIndex: 2 },
        { title: "Chapter 1", pageIndex: 2 },
        { title: "Chapter 2", pageIndex: 6 },
      ],
    });
    expect(item.chapters).toEqual([
      { title: "Chapter 1", startPage: 2, endPage: 5 },
      { title: "Chapter 2", startPage: 6, endPage: 9 },
    ]);
    expect(item.pageCount).toBe(10);
  });

  it("has no chapters without an outline", () => {
    expect(dialogItem("book", "B", { pageCount: 10, outline: null }).chapters).toBeNull();
  });
});
