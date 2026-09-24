import { describe, expect, it } from "vitest";
import { describeBar, nextSpeed, type BarInput } from "../src/bar";

const OWN = "zotero:ABC";

function input(change: Partial<BarInput> = {}): BarInput {
  return {
    adoption: { kind: "ready" },
    stream: "connected",
    problem: null,
    ownChannel: OWN,
    speakingChannel: "",
    paused: false,
    reading: false,
    speed: 1,
    ...change,
  };
}

describe("describeBar", () => {
  it("offers to start a read when nothing is speaking", () => {
    const view = describeBar(input());
    expect(view.kind).toBe("idle");
    expect(view.play).toMatchObject({ enabled: true, action: "start" });
    expect(view.back.enabled).toBe(false);
    expect(view.ahead.enabled).toBe(false);
    expect(view.stop.enabled).toBe(false);
    expect(view.slower.enabled).toBe(true);
  });

  it("gives pause, skips and stop while its own channel speaks", () => {
    const view = describeBar(input({ speakingChannel: OWN, reading: true }));
    expect(view.kind).toBe("own-speaking");
    expect(view.play).toMatchObject({ enabled: true, action: "pause" });
    expect(view.back.enabled).toBe(true);
    expect(view.ahead.enabled).toBe(true);
    expect(view.stop.enabled).toBe(true);
  });

  it("offers resume while its own channel is paused", () => {
    const view = describeBar(input({ speakingChannel: OWN, reading: true, paused: true }));
    expect(view.kind).toBe("own-paused");
    expect(view.play).toMatchObject({ enabled: true, action: "resume" });
  });

  it("keeps skips and pause off while another channel speaks, since they would move that one", () => {
    const waiting = describeBar(input({ speakingChannel: "agent:x", reading: true }));
    expect(waiting.kind).toBe("waiting");
    expect(waiting.play.enabled).toBe(false);
    expect(waiting.back.enabled).toBe(false);
    expect(waiting.stop.enabled).toBe(true);

    const other = describeBar(input({ speakingChannel: "agent:x" }));
    expect(other.kind).toBe("other-speaking");
    expect(other.play).toMatchObject({ enabled: true, action: "start" });
    expect(other.ahead.enabled).toBe(false);
  });

  it("says no daemon, and offers nothing", () => {
    const view = describeBar(input({ stream: "no-daemon", reading: true }));
    expect(view.kind).toBe("no-daemon");
    expect(view.status).toMatch(/not running/);
    for (const control of [view.play, view.back, view.ahead, view.stop, view.slower, view.faster]) {
      expect(control.enabled).toBe(false);
    }
  });

  it("says the token is wrong, from the stream or from a verb", () => {
    expect(describeBar(input({ stream: "bad-token" })).kind).toBe("bad-token");
    const fromVerb = describeBar(input({ stream: "connecting", problem: { kind: "bad-token", error: "401" } }));
    expect(fromVerb.kind).toBe("bad-token");
    expect(fromVerb.status).toMatch(/speakctl http-token/);
    expect(fromVerb.play.enabled).toBe(false);
  });

  it("lets a read that failed on an old token or a lost daemon be tried again once connected", () => {
    // A connected stream is the daemon, taking the token now in the preferences.
    for (const kind of ["bad-token", "no-daemon"] as const) {
      const view = describeBar(input({ problem: { kind, error: "then" } }));
      expect(view.kind).toBe("problem");
      expect(view.play).toMatchObject({ enabled: true, action: "start" });
    }
  });

  it("says when the daemon declined, and lets the user try again", () => {
    const view = describeBar(input({ problem: { kind: "declined", error: "muted" } }));
    expect(view.kind).toBe("problem");
    expect(view.status).toMatch(/muted/);
    expect(view.play).toMatchObject({ enabled: true, action: "start" });
  });

  it("says why a reader cannot be read, and offers nothing", () => {
    const refused = describeBar(input({ adoption: { kind: "refused", reason: "missing manager._createController" } }));
    expect(refused.kind).toBe("refused");
    expect(refused.status).toMatch(/_createController/);
    expect(refused.play.enabled).toBe(false);
    expect(describeBar(input({ adoption: { kind: "unadopted" } })).status).toMatch(/reopen/i);
  });

  it("shows the speed, and stops offering to go past speakd's bounds", () => {
    expect(describeBar(input({ speed: 1.25 })).speed).toBe("1.25×");
    expect(describeBar(input({ speed: null })).speed).toBe("–");
    expect(describeBar(input({ speed: 0.7 })).slower.enabled).toBe(false);
    expect(describeBar(input({ speed: 1.6 })).faster.enabled).toBe(false);
  });
});

describe("nextSpeed", () => {
  it("steps by a tenth, on the tenths, within speakd's bounds", () => {
    expect(nextSpeed(1, 1)).toBe(1.1);
    expect(nextSpeed(1.25, -1)).toBe(1.2);
    expect(nextSpeed(1.25, 1)).toBe(1.3);
    expect(nextSpeed(0.7, -1)).toBe(0.7);
    expect(nextSpeed(1.6, 1)).toBe(1.6);
    expect(nextSpeed(null, 1)).toBe(1.1);
  });
});
