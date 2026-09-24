import { describe, expect, it } from "vitest";
import type { Problem } from "../src/channel";
import { CONTROLLER_WAIT_MS, ReaderSession, resumesOnStart, type ChannelLike, type SegmentInfo, type SessionHooks } from "../src/session";
import type { CallResult, SpeakdEvent } from "../src/speakd";
import type { SegmentText } from "../src/sections";

const OK: CallResult = { kind: "ok", data: {} };

// Stands in for channel.ts: records what it was asked, and lets the test
// say what the daemon is doing.
class FakeChannel implements ChannelLike {
  log: string[] = [];
  reading = false;
  speaking = false;
  paused = false;
  lastIndex: number | null = null;
  started: { segments: readonly SegmentText[]; from: number }[] = [];
  private highlightListener: ((index: number | null) => void) | null = null;
  private endListener: (() => void) | null = null;
  private problemListener: ((problem: Problem) => void) | null = null;

  async start(segments: readonly SegmentText[], from: number): Promise<void> {
    this.log.push(`start ${from}`);
    this.started.push({ segments, from });
    this.reading = true;
  }
  async idle(): Promise<void> {}
  async stop(options: { always?: boolean } = {}): Promise<void> {
    this.log.push(options.always === true ? "stop always" : "stop");
    this.reading = false;
  }
  async skip(by: number): Promise<CallResult> {
    this.log.push(`skip ${by}`);
    return OK;
  }
  async pause(): Promise<CallResult> {
    this.log.push("pause");
    return OK;
  }
  async resume(): Promise<CallResult> {
    this.log.push("resume");
    return OK;
  }
  streamLost(): void {
    this.log.push("stream lost");
    if (this.reading) this.problem({ kind: "no-daemon", error: "the connection to speakd was lost" });
    this.speaking = false;
  }
  handleEvent(event: SpeakdEvent): void {
    if (event.event === "transport") this.paused = event.data.paused === true;
  }
  onHighlight(listener: (index: number | null) => void): () => void {
    this.highlightListener = listener;
    return () => (this.highlightListener = null);
  }
  onEnd(listener: () => void): () => void {
    this.endListener = listener;
    return () => (this.endListener = null);
  }
  onProblem(listener: (problem: Problem) => void): () => void {
    this.problemListener = listener;
    return () => (this.problemListener = null);
  }

  highlight(index: number | null): void {
    if (index !== null) this.lastIndex = index;
    this.highlightListener?.(index);
  }
  end(): void {
    this.reading = false;
    this.speaking = false;
    this.endListener?.();
  }
  problem(problem: Problem): void {
    this.reading = false;
    this.problemListener?.(problem);
  }
}

// Eight segments in three paragraphs: [0,1,2] [3,4,5] [6,7].
const SEGMENTS: SegmentInfo[] = [
  "Zero is first.",
  "One follows.",
  "Two ends a paragraph.",
  "Three starts one.",
  "Four is here.",
  "Five ends it.",
  "Six starts the last.",
  "Seven ends the paper.",
].map((text, index) => ({ text, anchor: [0, 3, 6].includes(index) ? "paragraphStart" : null }));

function setup() {
  const channel = new FakeChannel();
  const tasks: (() => void)[] = [];
  const timers: { task: () => void; ms: number }[] = [];
  // Time passing: every timer set so far fires.
  const elapse = () => {
    for (const timer of timers.splice(0)) timer.task();
  };
  const hooks: SessionHooks & { log: string[] } = {
    log: [],
    defer: (task) => tasks.push(task),
    later: (task, ms) => {
      const timer = { task, ms };
      timers.push(timer);
      return () => timers.splice(timers.indexOf(timer), 1);
    },
    takeover: (active) => hooks.log.push(active ? "takeover on" : "takeover off"),
    mirrorPause: (paused) => hooks.log.push(paused ? "mirror pause" : "mirror play"),
  };
  const session = new ReaderSession(channel, hooks);
  const emitted: string[] = [];
  const sink = { emit: (type: string, index: number | null) => emitted.push(index === null ? type : `${type} ${index}`) };
  // What the event loop does between Zotero's synchronous calls.
  const tick = () => {
    while (tasks.length) tasks.shift()!();
  };
  // Zotero's _createController: the controller, then its paused state.
  const create = (back: number | null, paused = false) => {
    const controller = session.createController(SEGMENTS, back, null, sink);
    controller.paused = paused;
    return controller;
  };
  return { channel, hooks, session, emitted, sink, tick, create, elapse, timers };
}

describe("ReaderSession", () => {
  it("starts the channel at the controller's start index once Zotero is done building it", () => {
    const { channel, tick, create } = setup();
    create(2);
    expect(channel.log).toEqual([]);
    tick();
    expect(channel.log).toEqual(["start 2"]);
    expect(channel.started[0]!.segments).toHaveLength(8);
  });

  it("starts at the first segment when Zotero gives no index", () => {
    const { channel, tick, create } = setup();
    create(null);
    tick();
    expect(channel.log).toEqual(["start 0"]);
  });

  it("does not start a controller that Zotero built paused, until it is played", () => {
    const { channel, tick, create } = setup();
    const controller = create(4, true);
    tick();
    expect(channel.log).toEqual([]);
    controller.paused = false;
    tick();
    expect(channel.log).toEqual(["start 4"]);
  });

  it("highlights the segment the channel names, and ignores the channel's clearing", () => {
    const { channel, emitted, tick, create } = setup();
    create(0);
    tick();
    channel.highlight(1);
    channel.highlight(null);
    channel.highlight(2);
    expect(emitted).toEqual(["ActiveSegmentChange 1", "ActiveSegmentChange 2"]);
  });

  it("treats a controller replaced in the same tick as a jump, not a stop", () => {
    const { channel, hooks, session, tick, create } = setup();
    session.want({ kind: "here" });
    const first = create(0);
    tick();
    first.destroy();
    create(5);
    tick();
    expect(channel.log).toEqual(["start 0", "start 5"]);
    expect(hooks.log).toEqual(["takeover on"]);
    expect(session.wanted).toBe(true);
  });

  it("carries a read on through a rebuild at the sentence being read, rather than restarting it", () => {
    // Zotero rebuilds the controller whenever its voices finish loading
    // late (_resolveVoice -> _applyVoice), starting at the active segment.
    const { channel, emitted, session, tick, create } = setup();
    session.want({ kind: "selection", text: "One follows. Two ends a" });
    const first = create(1);
    tick();
    channel.highlight(2);
    first.destroy();
    const second = create(2);
    tick();
    expect(channel.log).toEqual(["start 1"]);
    channel.highlight(2);
    expect(emitted).toEqual(["ActiveSegmentChange 2", "ActiveSegmentChange 2"]);
    // Still the selection's read: it ends where the selection does.
    expect(second.end).toBe(2);
    channel.end();
    expect(emitted.at(-1)).toBe("Complete");
  });

  it("starts over when the rebuilt controller has new segments, even at the same index", () => {
    const { channel, session, tick } = setup();
    const sink = { emit: () => {} };
    const first = session.createController(SEGMENTS, 0, null, sink);
    tick();
    channel.highlight(2);
    first.destroy();
    session.createController([...SEGMENTS], 2, null, sink);
    tick();
    expect(channel.log).toEqual(["start 0", "start 2"]);
  });

  it("stops the channel and ends the takeover when a controller goes with no successor", () => {
    const { channel, hooks, session, tick, create } = setup();
    session.want({ kind: "here" });
    const controller = create(0);
    tick();
    controller.destroy();
    tick();
    expect(channel.log).toEqual(["start 0", "stop"]);
    expect(hooks.log).toEqual(["takeover on", "takeover off"]);
    expect(session.wanted).toBe(false);
  });

  it("sends a destroyed controller's highlights nowhere", () => {
    const { channel, emitted, tick, create } = setup();
    const controller = create(0);
    tick();
    controller.destroy();
    channel.highlight(3);
    expect(emitted).toEqual([]);
  });

  it("relays pause and resume only while its own channel is speaking", () => {
    const { channel, tick, create } = setup();
    const controller = create(0);
    tick();
    controller.paused = true;
    expect(channel.log).toEqual(["start 0"]);

    channel.speaking = true;
    controller.paused = true;
    channel.paused = true;
    controller.paused = true; // already paused: said once
    controller.paused = false;
    expect(channel.log).toEqual(["start 0", "pause", "resume"]);
  });

  it("plays a finished or failed read again from where it was", () => {
    const { channel, tick, create } = setup();
    const controller = create(2);
    tick();
    channel.highlight(4);
    channel.problem({ kind: "no-daemon", error: "refused" });
    controller.paused = false;
    expect(channel.log).toEqual(["start 2", "start 4"]);
  });

  it("tells Zotero a read is complete when the channel comes to its end, and plays it again from its start", () => {
    const { channel, emitted, tick, create } = setup();
    const controller = create(2);
    tick();
    channel.highlight(7);
    channel.end();
    expect(emitted).toEqual(["ActiveSegmentChange 7", "Complete"]);
    controller.paused = false;
    expect(channel.log).toEqual(["start 2", "start 2"]);
  });

  it("says Error on a problem, so Zotero shows the read paused", () => {
    const { channel, emitted, tick, create } = setup();
    const controller = create(0);
    tick();
    channel.problem({ kind: "no-daemon", error: "refused" });
    expect(emitted).toEqual(["Error"]);
    expect(controller.error).toBe("network");
  });

  it("skips a sentence or five through the channel's seek", () => {
    const { channel, tick, create } = setup();
    const controller = create(0);
    tick();
    channel.speaking = true;
    controller.skipAhead("sentence", false);
    controller.skipBack("sentence", true);
    expect(channel.log).toEqual(["start 0", "skip 1", "skip -5"]);
    expect(controller.lastSkipGranularity).toBe("sentence");
  });

  it("skips a paragraph by starting over at its first segment", () => {
    const { channel, tick, create } = setup();
    const controller = create(0);
    tick();
    channel.speaking = true;
    channel.highlight(4);
    controller.skipAhead("paragraph", false);
    channel.highlight(6);
    controller.skipBack("paragraph", false);
    expect(channel.log).toEqual(["start 0", "start 6", "start 3"]);
  });

  it("does not skip while another channel is speaking", () => {
    const { channel, tick, create } = setup();
    const controller = create(0);
    tick();
    controller.skipAhead("sentence", false);
    controller.skipAhead("paragraph", false);
    expect(channel.log).toEqual(["start 0"]);
  });

  it("reads a selection only to the segment where it ends", () => {
    const { channel, session, tick, create } = setup();
    session.want({ kind: "selection", text: "One follows. Two ends a" });
    create(1);
    tick();
    expect(channel.started[0]!.segments).toHaveLength(3);
    expect(channel.started[0]!.from).toBe(1);

    // A jump after it reads on to the end.
    create(5);
    tick();
    expect(channel.started[1]!.segments).toHaveLength(8);
  });

  it("starts a selection at its own first segment when Zotero lands on the one before", () => {
    const { channel, session, tick, create } = setup();
    session.want({ kind: "selection", text: "Two ends a paragraph. Three" });
    create(1);
    tick();
    expect(channel.started[0]!.from).toBe(2);
    expect(channel.started[0]!.segments).toHaveLength(4);

    // "Read from here" on a selection starts the same way, and reads on.
    session.want({ kind: "here", text: "Two ends a paragraph. Three" });
    create(1);
    tick();
    expect(channel.started[1]!.from).toBe(2);
    expect(channel.started[1]!.segments).toHaveLength(8);
  });

  it("hands Zotero the segment being read to annotate", () => {
    const { channel, tick, create } = setup();
    const controller = create(2);
    tick();
    expect(controller.segmentToAnnotate()).toBe(2);
    channel.highlight(3);
    expect(controller.segmentToAnnotate()).toBe(3);
  });

  it("keeps Zotero's speed to itself: the speed is the listener's, set elsewhere", () => {
    const { channel, tick, create } = setup();
    const controller = create(0);
    tick();
    controller.speed = 1.5;
    expect(controller.speed).toBe(1.5);
    expect(channel.log).toEqual(["start 0"]);
  });

  it("mirrors the daemon's pause into Zotero while its own channel speaks", () => {
    const { channel, hooks, session, tick, create } = setup();
    create(0);
    tick();
    session.handleEvent({ event: "transport", source_id: "", data: { paused: true } });
    expect(hooks.log).toEqual([]);
    channel.speaking = true;
    session.handleEvent({ event: "transport", source_id: "", data: { paused: true } });
    session.handleEvent({ event: "transport", source_id: "", data: { paused: false } });
    expect(hooks.log).toEqual(["mirror pause", "mirror play"]);
  });

  it("stops once when its own stop is pressed, whatever Zotero then destroys", () => {
    const { channel, hooks, session, tick, create } = setup();
    session.want({ kind: "here" });
    const controller = create(0);
    tick();
    session.stop();
    controller.destroy();
    tick();
    expect(channel.log).toEqual(["start 0", "stop always"]);
    expect(hooks.log).toEqual(["takeover on", "takeover off"]);
  });

  it("stops once when the tab closes, and hears nothing after", () => {
    const { channel, emitted, session, tick, create } = setup();
    create(0);
    tick();
    session.close();
    session.close();
    channel.highlight(2);
    expect(channel.log).toEqual(["start 0", "stop"]);
    expect(emitted).toEqual([]);
  });

  it("reports the last problem, and forgets it on the next start", () => {
    const { channel, session, tick, create } = setup();
    create(0);
    tick();
    channel.problem({ kind: "bad-token", error: "401" });
    expect(session.problem?.kind).toBe("bad-token");
    session.want({ kind: "here" });
    expect(session.problem).toBeNull();
  });

  it("tells Zotero the read failed when the stream is lost mid-read", () => {
    const { channel, emitted, session, tick, create } = setup();
    create(0);
    tick();
    session.streamLost();
    expect(channel.log).toEqual(["start 0", "stream lost"]);
    expect(emitted).toEqual(["Error"]);
    expect(session.problem?.kind).toBe("no-daemon");
  });

  it("resumes on start only when nothing is speaking or this reader is", () => {
    expect(resumesOnStart("", "zotero:A")).toBe(true);
    expect(resumesOnStart("zotero:A", "zotero:A")).toBe(true);
    // An agent the user paused stays paused.
    expect(resumesOnStart("claude:session", "zotero:A")).toBe(false);
    expect(resumesOnStart("zotero:B", "zotero:A")).toBe(false);
  });

  it("asks the channel to hush on its own stop even with no read it knows of", () => {
    const { channel, session } = setup();
    session.stop();
    expect(channel.log).toEqual(["stop always"]);
  });

  it("gives the takeover up, and says so, when Zotero builds no controller in time", () => {
    const { hooks, session, elapse, timers } = setup();
    session.want({ kind: "here" });
    expect(timers.map((timer) => timer.ms)).toEqual([CONTROLLER_WAIT_MS]);
    elapse();
    expect(session.wanted).toBe(false);
    expect(hooks.log).toEqual(["takeover on", "takeover off"]);
    expect(session.problem).toEqual({ kind: "zotero", error: "Zotero's Read Aloud did not start" });
  });

  it("keeps the takeover when the controller comes in time", () => {
    const { hooks, session, tick, create, elapse } = setup();
    session.want({ kind: "here" });
    create(0);
    tick();
    elapse();
    expect(session.wanted).toBe(true);
    expect(hooks.log).toEqual(["takeover on"]);
  });

  it("gives up at once, with the reason, when told Zotero will build none", () => {
    const { hooks, session, timers } = setup();
    session.want({ kind: "here" });
    session.giveUp({ kind: "zotero", error: "no voice" });
    expect(timers).toEqual([]);
    expect(hooks.log).toEqual(["takeover on", "takeover off"]);
    expect(session.problem).toEqual({ kind: "zotero", error: "no voice" });
  });

  it("does not give up a read whose controller has come: that one is the channel's to end", () => {
    const { hooks, session, tick, create } = setup();
    session.want({ kind: "here" });
    create(0);
    tick();
    session.giveUp({ kind: "zotero", error: "no voice" });
    expect(session.wanted).toBe(true);
    expect(hooks.log).toEqual(["takeover on"]);
  });
});
