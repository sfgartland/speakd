import { describe, expect, it } from "vitest";
import { resolveOutline, type RawOutlineItem, type OutlineDocument } from "../../src/export/structure";

// A pdf.js document over a table of named destinations and page refs.
function fakeDocument(): OutlineDocument {
  const refs: Record<string, number> = { r1: 0, r2: 4, r3: 9 };
  return {
    getDestination: async (name) => (name === "named" ? [{ ref: "r2" }, "XYZ"] : null),
    getPageIndex: async (ref) => {
      const page = refs[(ref as { ref: string }).ref];
      if (page === undefined) throw new Error("bad ref");
      return page;
    },
  };
}

describe("resolveOutline", () => {
  it("resolves explicit, named and numeric destinations to page indices", async () => {
    const raw: RawOutlineItem[] = [
      { title: "One", dest: [{ ref: "r1" }, "XYZ"], items: [] },
      { title: "Two", dest: "named", items: [] },
      { title: "Three", dest: [7, "Fit"], items: [] },
    ];
    expect(await resolveOutline(raw, fakeDocument())).toEqual([
      { title: "One", pageIndex: 0 },
      { title: "Two", pageIndex: 4 },
      { title: "Three", pageIndex: 7 },
    ]);
  });

  it("drops entries whose destination cannot be placed, and keeps the rest", async () => {
    const raw: RawOutlineItem[] = [
      { title: "Lost", dest: "missing", items: [] },
      { title: "Bad", dest: [{ ref: "nope" }], items: [] },
      { title: "Url", dest: null, items: [] },
      { title: "Fine", dest: [{ ref: "r3" }], items: [] },
    ];
    expect(await resolveOutline(raw, fakeDocument())).toEqual([{ title: "Fine", pageIndex: 9 }]);
  });

  it("orders by page and trims titles, dropping blank ones", async () => {
    const raw: RawOutlineItem[] = [
      { title: " Late ", dest: [{ ref: "r3" }], items: [] },
      { title: "  ", dest: [{ ref: "r1" }], items: [] },
      { title: "Early", dest: [{ ref: "r2" }], items: [] },
    ];
    expect((await resolveOutline(raw, fakeDocument()))!.map((item) => item.title)).toEqual(["Early", "Late"]);
  });

  it("answers null for no outline", async () => {
    expect(await resolveOutline(null, fakeDocument())).toBeNull();
    expect(await resolveOutline([], fakeDocument())).toBeNull();
  });
});
