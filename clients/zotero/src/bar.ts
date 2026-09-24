// What the control bar shows and offers, worked out from what the daemon has
// said -- never from what the plugin guesses it is doing.
//
// Pure: controls.ts renders the view this returns.

import type { Problem } from "./channel";
import type { StreamState } from "./speakd";

export type Adoption =
  | { kind: "ready" }
  | { kind: "pending" }
  /** A Zotero internal the takeover needs is missing: this reader stays Zotero's own. */
  | { kind: "refused"; reason: string }
  /** Opened before the plugin started, so its Read Aloud cannot be taken over. */
  | { kind: "unadopted" };

export interface BarInput {
  adoption: Adoption;
  stream: StreamState;
  /** Why this reader's last read failed, if it did. */
  problem: Problem | null;
  ownChannel: string;
  speakingChannel: string;
  paused: boolean;
  /** Whether this reader has a read under way (queued or speaking). */
  reading: boolean;
  speed: number | null;
}

export type BarKind =
  | "own-speaking"
  | "own-paused"
  | "waiting"
  | "other-speaking"
  | "idle"
  | "problem"
  | "no-daemon"
  | "bad-token"
  | "connecting"
  | "pending"
  | "refused"
  | "unadopted";

export interface Control {
  enabled: boolean;
}

export interface PlayControl extends Control {
  action: "start" | "pause" | "resume";
}

export interface BarView {
  kind: BarKind;
  status: string;
  play: PlayControl;
  back: Control;
  ahead: Control;
  stop: Control;
  slower: Control;
  faster: Control;
  speed: string;
}

/** speakd's bounds on the listener's speed (src/speakd/tempo.py). */
export const MIN_SPEED = 0.7;
export const MAX_SPEED = 1.6;

/** One step slower or faster, landing on the tenths, within speakd's bounds. */
export function nextSpeed(current: number | null, direction: 1 | -1): number {
  const from = current ?? 1;
  const tenths = direction === 1 ? Math.floor(from * 10 + 1e-6) + 1 : Math.ceil(from * 10 - 1e-6) - 1;
  return Math.min(MAX_SPEED, Math.max(MIN_SPEED, Math.round(tenths) / 10));
}

const off: Control = { enabled: false };

function silent(kind: BarKind, status: string): BarView {
  return {
    kind,
    status,
    play: { enabled: false, action: "start" },
    back: off,
    ahead: off,
    stop: off,
    slower: off,
    faster: off,
    speed: "–",
  };
}

export function describeBar(input: BarInput): BarView {
  const { adoption } = input;
  if (adoption.kind === "refused") return silent("refused", `speakd cannot read in this Zotero: ${adoption.reason}`);
  if (adoption.kind === "unadopted") return silent("unadopted", "speakd: reopen this tab to read it aloud");
  if (adoption.kind === "pending") return silent("pending", "speakd: getting ready…");
  if (input.stream === "bad-token" || input.problem?.kind === "bad-token") {
    return silent("bad-token", "speakd: wrong token — paste the output of speakctl http-token into Settings → speakd reader");
  }
  if (input.stream === "no-daemon" || input.problem?.kind === "no-daemon") {
    return silent("no-daemon", "speakd is not running");
  }
  if (input.stream === "connecting") return silent("connecting", "speakd: connecting…");

  const speed = input.speed;
  const common = {
    slower: { enabled: speed === null || speed > MIN_SPEED + 1e-6 },
    faster: { enabled: speed === null || speed < MAX_SPEED - 1e-6 },
    speed: speed === null ? "–" : `${Number(speed.toFixed(2))}×`,
  };
  const own = input.speakingChannel === input.ownChannel;
  const other = input.speakingChannel !== "" && !own;

  if (own) {
    return {
      ...common,
      kind: input.paused ? "own-paused" : "own-speaking",
      status: input.paused ? "paused" : "reading",
      play: { enabled: true, action: input.paused ? "resume" : "pause" },
      back: { enabled: true },
      ahead: { enabled: true },
      stop: { enabled: true },
    };
  }
  if (input.reading) {
    return {
      ...common,
      kind: other ? "waiting" : "idle",
      status: other ? "queued: another channel is speaking" : "starting…",
      play: { enabled: false, action: "pause" },
      back: off,
      ahead: off,
      stop: { enabled: true },
    };
  }
  const base = { ...common, play: { enabled: true, action: "start" as const }, back: off, ahead: off, stop: off };
  if (input.problem !== null) {
    return { ...base, kind: "problem", status: `speakd did not read it: ${input.problem.error}` };
  }
  if (other) return { ...base, kind: "other-speaking", status: "another channel is speaking" };
  return { ...base, kind: "idle", status: "" };
}
