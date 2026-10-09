// Scoring the deterministic rules against recorded ground truth (spec §8.3,
// §12). The tuning metric: the rules decide *detection*, the policy decides
// fate, so only detection is scored. `labels.json` is ground truth; the
// `unaided` tags are what the shipped rules produce with no overrides.
//
// Only labelled segments count: an unlabelled segment is not asserted
// ground truth and is counted neither way.

import type { PlanSegment } from "./footnotes";
import type { LabelCategory } from "./labelstore";
import type { TaggedSegment } from "./sections";

/** One ground-truth label, by original captured index. */
export interface FixtureLabel {
  readonly index: number;
  readonly category: LabelCategory;
}

/** The rules' score against a fixture's ground truth. */
export interface Score {
  /** Drops that are ground-truth non-body, over all counted drops; 1 when there are none. */
  readonly precision: number;
  /** Ground-truth non-body segments the rules caught, over all of them; 1 when there are none. */
  readonly recall: number;
  /** Ground-truth non-body segments the rules left spoken, by index. */
  readonly misses: readonly number[];
  /** Segments the rules dropped whose ground truth is body, by index. */
  readonly overDrops: readonly number[];
  /** Ground-truth footnotes the rules detected (a candidate at all). */
  readonly footnoteDetected: number;
  /** All ground-truth footnotes. */
  readonly footnoteTotal: number;
  /** Counted drops that are ground-truth non-body (running head, footer, page number, junk). */
  readonly correctDrops: number;
  /** All counted drops, footnote fates excluded (they are the policy's). */
  readonly drops: number;
}

/** Ground truths scored as non-body; a heading is informational and spoken in v1. */
const NON_BODY: ReadonlySet<LabelCategory> = new Set(["footnote", "running-head", "footer", "page-number", "junk"]);
/** Non-body truths the rules detect by dropping. */
const DROP_TRUTHS: ReadonlySet<LabelCategory> = new Set(["running-head", "footer", "page-number", "junk"]);

/**
 * Scores the unaided rules over `segments` against `labels`. `segments` is
 * the captured list, used to ignore a label whose index is out of range.
 */
export function scoreFixture(
  segments: readonly PlanSegment[],
  labels: readonly FixtureLabel[],
  unaided: readonly TaggedSegment[],
): Score {
  let caught = 0;
  let total = 0;
  let correctDrops = 0;
  let drops = 0;
  let footnoteDetected = 0;
  let footnoteTotal = 0;
  const misses: number[] = [];
  const overDrops: number[] = [];

  for (const { index, category } of labels) {
    if (index < 0 || index >= segments.length) continue;
    const tag = unaided[index]?.tag ?? "body";
    if (!NON_BODY.has(category)) {
      // A body truth the rules dropped is an over-drop; a heading is not scored.
      if (category === "body" && tag === "drop") {
        drops++;
        overDrops.push(index);
      }
      continue;
    }

    total++;
    if (category === "footnote") {
      footnoteTotal++;
      // Detection is "tagged anything but body"; the drop/defer/classify
      // distinction is the policy's, and is not scored here.
      if (tag !== "body") {
        caught++;
        footnoteDetected++;
      } else {
        misses.push(index);
      }
      continue;
    }

    // A running head, footer, page number or junk: detected only by a drop.
    if (tag === "drop") {
      caught++;
      correctDrops++;
      drops++;
    } else {
      misses.push(index);
    }
  }

  return {
    precision: drops === 0 ? 1 : correctDrops / drops,
    recall: total === 0 ? 1 : caught / total,
    misses,
    overDrops,
    footnoteDetected,
    footnoteTotal,
    correctDrops,
    drops,
  };
}
