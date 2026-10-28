import { describe, expect, it } from "vitest";
import { scoreFixture, type FixtureLabel } from "../src/scoring";
import type { TaggedSegment } from "../src/sections";

function tagged(tags: readonly string[]): TaggedSegment[] {
  return tags.map((tag) => ({ text: `s${tag}`, tag: tag as TaggedSegment["tag"] }));
}

const segments = [0, 1, 2, 3, 4, 5].map((n) => ({ text: `segment ${n}`, pageIndex: 0 }));

describe("scoreFixture", () => {
  it("reports the planted misses and over-drops, and the precision and recall", () => {
    const labels: FixtureLabel[] = [
      { index: 0, category: "body" },
      { index: 1, category: "running-head" }, // spoken: a miss
      { index: 2, category: "footnote" }, // dropped: detected
      { index: 3, category: "footnote" }, // spoken: a miss, and not detected
      { index: 4, category: "body" }, // dropped: an over-drop
    ];
    const score = scoreFixture(segments, labels, tagged(["body", "body", "drop", "body", "drop"]));
    expect(score.misses).toEqual([1, 3]);
    expect(score.overDrops).toEqual([4]);
    expect(score.footnoteDetected).toBe(1);
    expect(score.footnoteTotal).toBe(2);
    // The only counted drop is the body over-drop (footnote fates are the policy's).
    expect(score.drops).toBe(1);
    expect(score.correctDrops).toBe(0);
    expect(score.precision).toBe(0);
    // Non-body truth: 1, 2, 3; only 2 is caught.
    expect(score.recall).toBeCloseTo(1 / 3);
  });

  it("counts unlabeled segments neither way", () => {
    const labels: FixtureLabel[] = [{ index: 0, category: "body" }];
    const score = scoreFixture(segments, labels, tagged(["body", "drop", "drop", "drop", "drop", "drop"]));
    expect(score.misses).toEqual([]);
    expect(score.overDrops).toEqual([]);
    expect(score.drops).toBe(0);
    expect(score.precision).toBe(1);
    expect(score.recall).toBe(1);
  });

  it("does not miss a detected footnote the policy kept", () => {
    const labels: FixtureLabel[] = [{ index: 0, category: "footnote" }];
    const score = scoreFixture(segments, labels, tagged(["defer"]));
    expect(score.misses).toEqual([]);
    expect(score.footnoteDetected).toBe(1);
    expect(score.recall).toBe(1);
  });

  it("scores a zero-disagreement fixture 1.0 and 1.0", () => {
    const labels: FixtureLabel[] = [
      { index: 0, category: "body" },
      { index: 1, category: "running-head" },
      { index: 2, category: "footnote" },
    ];
    const score = scoreFixture(segments, labels, tagged(["body", "drop", "drop"]));
    expect(score.misses).toEqual([]);
    expect(score.overDrops).toEqual([]);
    expect(score.precision).toBe(1);
    expect(score.recall).toBe(1);
  });
});
