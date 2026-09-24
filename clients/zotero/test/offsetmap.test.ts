import { describe, expect, it } from "vitest";
import { apply, compose, diff, identity, lookup, stageFromDiff, verify } from "../src/offsetmap";

// Every output character of `output` that the diff kept, paired with the
// input character it came from, so a test can say "these letters were not
// moved" without spelling out the whole map.
function kept(input: string, output: string, map: readonly number[]): string[] {
  const pairs: string[] = [];
  for (let i = 0; i < output.length; i++) {
    const j = map[i]!;
    if (input[j] === output[i]) pairs.push(`${output[i]}@${j}`);
  }
  return pairs;
}

describe("identity", () => {
  it("maps every index to itself, with the end as the last entry", () => {
    expect(identity(3)).toEqual([0, 1, 2, 3]);
    expect(verify(identity(3), 3, 3)).toBe(true);
  });

  it("is a single end entry for the empty string", () => {
    expect(identity(0)).toEqual([0]);
  });
});

describe("diff", () => {
  it("is the identity for equal strings", () => {
    expect(diff("abc", "abc")).toEqual([0, 1, 2, 3]);
  });

  it("maps around a deletion", () => {
    // "hyphen- ation" -> "hyphenation": the "- " at 6..7 is gone.
    const map = diff("hyphen- ation", "hyphenation")!;
    expect(map).toEqual([0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13]);
    expect(verify(map, 13, 11)).toBe(true);
  });

  it("keeps the characters an insertion leaves alone, and stays monotone", () => {
    const input = "Fig. 3";
    const output = "Figure 3";
    const map = diff(input, output)!;
    expect(verify(map, input.length, output.length)).toBe(true);
    expect(map[0]).toBe(0);
    expect(map[2]).toBe(2);
    expect(map[6]).toBe(4); // the space
    expect(map[7]).toBe(5); // the "3"
    expect(map[8]).toBe(6); // the end
  });

  it("maps a replacement monotonically, keeping what both sides share", () => {
    const input = "see et al. for more";
    const output = "see and others for more";
    const map = diff(input, output)!;
    expect(verify(map, input.length, output.length)).toBe(true);
    expect(kept(input, output, map).slice(0, 4)).toEqual(["s@0", "e@1", "e@2", " @3"]);
    expect(map[output.indexOf("for")]).toBe(input.indexOf("for"));
  });

  it("handles one side being empty", () => {
    expect(diff("", "ab")).toEqual([0, 0, 0]);
    expect(diff("ab", "")).toEqual([2]);
  });

  it("gives a verified map for many random rewrites", () => {
    // A small deterministic generator, so a failure is reproducible.
    let seed = 7;
    const random = (): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    const word = (): string => "abc -.".charAt(Math.floor(random() * 6));
    for (let round = 0; round < 200; round++) {
      const input = Array.from({ length: Math.floor(random() * 40) }, word).join("");
      const output = Array.from({ length: Math.floor(random() * 40) }, word).join("");
      const map = diff(input, output)!;
      expect(verify(map, input.length, output.length)).toBe(true);
    }
  });

  it("gives up rather than align two unrelated texts at great cost", () => {
    expect(diff("a".repeat(50), "b".repeat(50), 10)).toBeNull();
  });
});

describe("compose", () => {
  it("maps through both stages", () => {
    // Stage one drops the "- ", stage two drops the trailing "!".
    const first = diff("hyphen- ation!", "hyphenation!")!;
    const second = diff("hyphenation!", "hyphenation")!;
    const both = compose(first, second);
    expect(both).toEqual(diff("hyphen- ation!", "hyphenation"));
    expect(verify(both, 14, 11)).toBe(true);
  });
});

describe("verify", () => {
  it("refuses a map of the wrong length", () => {
    expect(verify([0, 1], 1, 2)).toBe(false);
  });

  it("refuses a map that goes backwards", () => {
    expect(verify([0, 2, 1, 3], 3, 3)).toBe(false);
  });

  it("refuses a map that points past the input", () => {
    expect(verify([0, 1, 5, 3], 3, 3)).toBe(false);
  });

  it("refuses a map whose end is not the input's end", () => {
    expect(verify([0, 1, 2, 2], 3, 3)).toBe(false);
  });

  it("refuses a map with a fractional entry", () => {
    expect(verify([0, 0.5, 2, 3], 3, 3)).toBe(false);
  });
});

describe("apply", () => {
  it("passes a stage whose map verifies", () => {
    const stage = { text: "ab", map: [0, 2, 3] };
    expect(apply("axb", stage)).toBe(stage);
  });

  it("falls back to the stage's input, unchanged, when its map is wrong", () => {
    const result = apply("axb", { text: "ab", map: [0, 2] });
    expect(result).toEqual({ text: "axb", map: [0, 1, 2, 3] });
  });
});

describe("stageFromDiff", () => {
  it("builds a verified stage from two texts", () => {
    const stage = stageFromDiff("a  b", "a b");
    expect(stage.text).toBe("a b");
    expect(verify(stage.map, 4, 3)).toBe(true);
  });

  it("falls back to the input when the diff gives up", () => {
    const stage = stageFromDiff("a".repeat(50), "b".repeat(50), 10);
    expect(stage).toEqual({ text: "a".repeat(50), map: identity(50) });
  });
});

describe("lookup", () => {
  it("clamps an index outside the map to its ends", () => {
    const map = [2, 3, 5];
    expect(lookup(map, -1)).toBe(2);
    expect(lookup(map, 1)).toBe(3);
    expect(lookup(map, 9)).toBe(5);
  });
});
