import { describe, expect, it } from "vitest";
import { exportTarget, type MenuItem } from "../../src/export/menu";

function pdf(id: number, parentID: number | null = null): MenuItem {
  return { id, isAttachment: true, contentType: "application/pdf", isRegular: false, parentID, childIDs: [] };
}
function regular(id: number, childIDs: number[], itemType = "journalArticle"): MenuItem {
  return { id, isAttachment: false, contentType: null, isRegular: true, parentID: null, childIDs, itemType };
}

describe("exportTarget", () => {
  const library = new Map<number, MenuItem>([
    [1, regular(1, [2])],
    [2, pdf(2, 1)],
    [3, regular(3, [4, 5], "book")],
    [4, pdf(4, 3)],
    [5, pdf(5, 3)],
    [6, { ...pdf(6), contentType: "text/html" }],
    [7, regular(7, [6])],
    [8, regular(8, [])],
    [9, pdf(9)],
  ]);
  const lookup = (id: number) => library.get(id) ?? null;
  const items = (...ids: number[]) => ids.map((id) => library.get(id)!);

  it("takes a PDF attachment, with its parent as the titled item", () => {
    expect(exportTarget(items(2), lookup)).toEqual({ attachmentID: 2, itemID: 1 });
  });
  it("takes a standalone PDF as its own item", () => {
    expect(exportTarget(items(9), lookup)).toEqual({ attachmentID: 9, itemID: 9 });
  });
  it("takes a regular item with exactly one PDF child", () => {
    expect(exportTarget(items(1), lookup)).toEqual({ attachmentID: 2, itemID: 1 });
  });
  it("refuses a regular item with several or no PDFs", () => {
    expect(exportTarget(items(3), lookup)).toBeNull();
    expect(exportTarget(items(7), lookup)).toBeNull();
    expect(exportTarget(items(8), lookup)).toBeNull();
  });
  it("refuses a non-PDF attachment", () => {
    expect(exportTarget(items(6), lookup)).toBeNull();
  });
  it("refuses none or several selected items", () => {
    expect(exportTarget([], lookup)).toBeNull();
    expect(exportTarget(items(1, 9), lookup)).toBeNull();
  });
});
