import { describe, expect, it } from "vitest";
import {
  cleanup,
  collapseWhitespace,
  dropPageNumbers,
  fixLigatures,
  joinHyphenation,
  ocrSuspicion,
} from "../src/cleanup";
import { lookup, verify } from "../src/offsetmap";

// Where in `input` the output's `word` came from, so a test can check that
// a span into cleaned text lands on the same word in the source.
function source(input: string, stage: { text: string; map: readonly number[] }, word: string) {
  const at = stage.text.indexOf(word);
  expect(at).toBeGreaterThanOrEqual(0);
  const from = lookup(stage.map, at);
  return input.slice(from, from + word.length);
}

describe("joinHyphenation", () => {
  it("joins a word hyphenated across a line break", () => {
    const input = "the hyphen-\nation of words";
    const stage = joinHyphenation(input);
    expect(stage.text).toBe("the hyphenation of words");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "of")).toBe("of");
  });

  it("joins one Zotero has already flattened to a space", () => {
    expect(joinHyphenation("a Heideg- gerian reading").text).toBe("a Heideggerian reading");
  });

  it("leaves a suspended hyphen before a conjunction alone", () => {
    for (const text of ["pre- and post-war", "Vor- und Nachteile", "first- or second-order"]) {
      expect(joinHyphenation(text).text).toBe(text);
    }
  });

  it("leaves hyphenated compounds and dashes alone", () => {
    for (const text of ["well-known", "a - b", "1990- 1995", "the X- Ray"]) {
      expect(joinHyphenation(text).text).toBe(text);
    }
  });
});

describe("collapseWhitespace", () => {
  it("collapses runs of whitespace to one space and trims the ends", () => {
    const input = "  one \n\t two   three ";
    const stage = collapseWhitespace(input);
    expect(stage.text).toBe("one two three");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "three")).toBe("three");
  });
});

describe("dropPageNumbers", () => {
  it("drops a bare number standing between two sentences", () => {
    const input = "the end of a page. 12 The next page begins.";
    const stage = dropPageNumbers(input);
    expect(stage.text).toBe("the end of a page. The next page begins.");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "The next")).toBe("The next");
  });

  it("drops one that opens or closes the text", () => {
    expect(dropPageNumbers("214 Thus it begins.").text).toBe("Thus it begins.");
    expect(dropPageNumbers("Thus it ends. 215").text).toBe("Thus it ends.");
  });

  it("keeps numbers that are part of a sentence", () => {
    for (const text of [
      "In 1994 Stiegler wrote.",
      "He wrote it in 1994. Then more.",
      "See chapter 3 for more.",
      "There were 12 of them.",
      "It ends. 12 of them stayed.",
    ]) {
      expect(dropPageNumbers(text).text).toBe(text);
    }
  });
});

describe("fixLigatures", () => {
  it("expands broken ligatures, mapping the expansion back into the ligature's span", () => {
    const input = "a ﬁle of ﬂow, an eﬀect of the oﬃce's staﬀ";
    const stage = fixLigatures(input);
    expect(stage.text).toBe("a file of flow, an effect of the office's staff");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    // Every character of the expansion resolves back into the ligature's own
    // span: a highlight over it lights the ligature word, never its neighbour.
    const at = stage.text.indexOf("flow");
    const start = input.indexOf("ﬂ");
    for (let i = 0; i < "flow".length; i++) {
      const back = lookup(stage.map, at + i);
      expect(back).toBeGreaterThanOrEqual(start);
      expect(back).toBeLessThan(start + "ﬂow".length);
    }
    expect(source(input, stage, "ow")).toBe("ow");
    expect(source(input, stage, "staff")).toBe("staﬀ");
  });

  it("removes zero-width, control and BOM characters and stray marks", () => {
    const input = "a\u200Bb\u0007c\uFEFFd\u00ADe\u00B7f\u00ACg";
    const stage = fixLigatures(input);
    expect(stage.text).toBe("abcdefg");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "e")).toBe("e");
  });

  it("leaves tabs, newlines and carriage returns to the whitespace rule", () => {
    const input = "one\ntwo\tthree\rfour";
    expect(fixLigatures(input).text).toBe(input);
  });
});

describe("ocrSuspicion", () => {
  it("is higher on an OCR-mangled text than on clean prose", () => {
    // The exact cut-off is provisional (a real scan fixture has not arrived);
    // what the tests pin is the ordering.
    const mangled =
      "Tbe modem ﬁle says tbat in 1 994 was tbe year of tbe scan\u00AC, \u00B7 and rn.";
    const clean =
      "The modern file says that in 1994 was the year of the edition, and the argument holds.";
    expect(ocrSuspicion(mangled)).toBeGreaterThan(ocrSuspicion(clean));
    expect(ocrSuspicion(clean)).toBeLessThan(ocrSuspicion(mangled));
  });

  it("counts a number broken by a space as a split-digit hit, not a range", () => {
    // "12- 20" is a range torn by the line break; "1 994" is a broken year.
    expect(ocrSuspicion("in 1 994 it began")).toBeGreaterThan(ocrSuspicion("pp. 12- 20 it ran"));
    expect(ocrSuspicion("pp. 12- 20 it ran")).toBeGreaterThanOrEqual(ocrSuspicion("pp. 12-20 it ran"));
  });

  it("is zero on clean prose with long words and no marks", () => {
    expect(ocrSuspicion("Philosophy reads argumentatively throughout")).toBe(0);
  });
});

describe("cleanup", () => {
  it("runs every rule, with one map from the result back to the input", () => {
    const input = "  a Heideg-\n gerian  reading. 17 Then   Dasein. ";
    const stage = cleanup(input);
    expect(stage.text).toBe("a Heideggerian reading. Then Dasein.");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "Then")).toBe("Then");
    expect(source(input, stage, "reading")).toBe("reading");
    expect(source(input, stage, "Heideg")).toBe("Heideg");
  });

  it("runs every rule, ligatures first, with one map from the result back to the input", () => {
    const input = "a ﬁle of ﬂow. 12 Next   page.";
    const stage = cleanup(input);
    expect(stage.text).toBe("a file of flow. Next page.");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
    expect(source(input, stage, "ow")).toBe("ow");
    expect(source(input, stage, "Next")).toBe("Next");
  });

  it("is the identity on text that needs no cleaning", () => {
    const stage = cleanup("Nothing to do here.");
    expect(stage.text).toBe("Nothing to do here.");
    expect(stage.map).toEqual(Array.from({ length: 20 }, (_, i) => i));
  });
});
