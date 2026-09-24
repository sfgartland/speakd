import { describe, expect, it } from "vitest";
import { cleanup } from "../src/cleanup";
import { planHighlights } from "../src/mapping";
import { identity } from "../src/offsetmap";
import { buildSections } from "../src/sections";

// The daemon's sentences over `text`, one per string in `sentences`, found
// in order: what `started.segments` would carry for that text.
function daemon(text: string, sentences: string[]) {
  let from = 0;
  return sentences.map((sentence, index) => {
    const start = text.indexOf(sentence, from);
    expect(start).toBeGreaterThanOrEqual(0);
    from = start + sentence.length;
    return { index, span_start: start, span_end: from };
  });
}

describe("planHighlights", () => {
  const segments = [{ text: "Zero." }, { text: "One is here." }, { text: "Two." }, { text: "Three, last." }];
  const [section] = buildSections(segments, 1, 3, 1000);

  it("maps each daemon sentence to the Zotero segment it starts in", () => {
    const text = section!.text;
    const plan = planHighlights(section!, daemon(text, ["One is here.", "Two.", "Three, last."]), identity(text.length));
    expect([...plan]).toEqual([
      [0, 1],
      [1, 2],
      [2, 3],
    ]);
  });

  it("maps a daemon sentence spanning two segments to the first", () => {
    const text = section!.text;
    const plan = planHighlights(section!, daemon(text, ["One is here. Two.", "Three, last."]), identity(text.length));
    expect(plan.get(0)).toBe(1);
    expect(plan.get(1)).toBe(3);
  });

  it("maps every piece of a segment the daemon split to that segment", () => {
    const text = section!.text;
    const plan = planHighlights(section!, daemon(text, ["One is", "here.", "Two.", "Three,", "last."]), identity(text.length));
    expect([...plan.values()]).toEqual([1, 1, 2, 3, 3]);
  });

  it("maps a span that starts on the space between two segments to the second", () => {
    const text = section!.text;
    const at = text.indexOf(" Two.");
    const plan = planHighlights(section!, [{ index: 0, span_start: at, span_end: at + 5 }], identity(text.length));
    expect(plan.get(0)).toBe(2);
  });

  it("maps through the cleanup stage's offset map", () => {
    const raw = [{ text: "A long hyphen-" }, { text: "ation here." }, { text: "12 Next page." }, { text: "Last one." }];
    const [joined] = buildSections(raw, 0, 4, 1000);
    const cleaned = cleanup(joined!.text);
    expect(cleaned.text).toBe("A long hyphenation here. Next page. Last one.");
    const plan = planHighlights(
      joined!,
      daemon(cleaned.text, ["A long hyphenation here.", "Next page.", "Last one."]),
      cleaned.map,
    );
    expect([...plan.values()]).toEqual([0, 2, 3]);
  });

  it("allows for text the daemon put before the section, such as a channel's label", () => {
    const text = section!.text;
    const spoken = `Paper: ${text}`;
    const shift = spoken.length - text.length;
    const plan = planHighlights(
      section!,
      daemon(spoken, ["Paper: One is here.", "Two.", "Three, last."]),
      identity(text.length),
      shift,
    );
    expect([...plan.values()]).toEqual([1, 2, 3]);
  });

  it("leaves out a sentence whose span lies outside the section's text", () => {
    const text = section!.text;
    const plan = planHighlights(
      section!,
      [
        { index: 0, span_start: 0, span_end: 5 },
        { index: 1, span_start: text.length, span_end: text.length + 4 },
        { index: 2, span_start: -3, span_end: -1 },
      ],
      identity(text.length),
    );
    expect([...plan.keys()]).toEqual([0]);
  });
});

describe("the mis-mapping defence", () => {
  // Two Zotero segments, and a map that is wrong: it sends every cleaned
  // character to the second segment. Offsets alone would light the wrong one.
  const section = buildSections(
    [{ text: "Kant gives the definition." }, { text: "Hegel disagrees entirely." }],
    0,
  )[0]!;
  const wrong = Array.from({ length: section.text.length + 1 }, () => section.offsets[1]!);

  it("lights nothing when the words spoken are not in the segment chosen", () => {
    const plan = planHighlights(section, [
      { index: 0, span_start: 0, span_end: 26, text: "Kant gives the definition." },
    ], wrong);
    expect(plan.has(0)).toBe(false);
  });

  it("still lights a segment whose words match", () => {
    const plan = planHighlights(section, [
      { index: 0, span_start: 0, span_end: 26, text: "Kant gives the definition." },
      { index: 1, span_start: 27, span_end: 52, text: "Hegel disagrees entirely." },
    ], identity(section.text.length));
    expect([plan.get(0), plan.get(1)]).toEqual([0, 1]);
  });

  it("trusts the offsets when speakd names no text", () => {
    const plan = planHighlights(section, [{ index: 0, span_start: 0, span_end: 26 }], identity(section.text.length));
    expect(plan.get(0)).toBe(0);
  });
});

describe("the mis-mapping defence, for a sentence that runs past its segment", () => {
  // A heading with no full stop: speakd reads it into the sentence after it.
  const section = buildSections([{ text: "Methods" }, { text: "We collected the data." }, { text: "Then more." }], 0, 3, 1000)[0]!;

  // speakd's sentences over the section, with the text `started` names for each.
  function spoken(sentences: string[]) {
    return daemon(section.text, sentences).map((segment, index) => ({ ...segment, text: sentences[index] }));
  }

  it("lights a short heading that a sentence begins with", () => {
    const plan = planHighlights(section, spoken(["Methods We collected the data.", "Then more."]), identity(section.text.length));
    expect([plan.get(0), plan.get(1)]).toEqual([0, 2]);
  });

  it("still lights nothing when the heading chosen is not what the sentence begins with", () => {
    // Offsets that went wrong: the second sentence sent to the heading.
    const plan = planHighlights(
      section,
      [{ index: 0, span_start: 0, span_end: 22, text: "We collected the data." }],
      identity(section.text.length),
    );
    expect(plan.has(0)).toBe(false);
  });

  it("still lights nothing when a sentence is sent to a longer segment that is not its own", () => {
    const at = section.offsets[1]!;
    const plan = planHighlights(section, [{ index: 0, span_start: at, span_end: at + 10, text: "Then more." }], identity(section.text.length));
    expect(plan.has(0)).toBe(false);
  });
});
