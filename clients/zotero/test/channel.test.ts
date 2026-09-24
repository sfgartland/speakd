import { describe, expect, it } from "vitest";
import { Channel, type Problem } from "../src/channel";
import type { CallResult, SpeakdEvent } from "../src/speakd";

const SOURCE = "zotero:ABC123";

interface Call {
  verb: string;
  sourceId: string;
  payload: Record<string, unknown>;
}

// A stand-in for the daemon: it records every verb, answers each with
// `answer` (ok by default), and replays the events a daemon would publish
// for what was enqueued -- `started` with speakd's sentence spans, then
// `position` and `finished` -- when the test says so.
class FakeDaemon {
  readonly calls: Call[] = [];
  answer: (call: Call) => CallResult = (call) =>
    call.verb === "enqueue" ? { kind: "ok", data: { spoken: true } } : { kind: "ok", data: {} };
  channel!: Channel;
  /** Called as a verb arrives, before it is answered: what the daemon publishes meanwhile. */
  during: (call: Call) => void = () => {};

  readonly client = {
    call: async (verb: string, sourceId: string, payload: Record<string, unknown> = {}): Promise<CallResult> => {
      const call = { verb, sourceId, payload };
      this.calls.push(call);
      this.during(call);
      // Answered on a later turn, as a real request would be, so that
      // anything relying on answers arriving at once is caught.
      await new Promise((resolve) => setTimeout(resolve, 0));
      return this.answer(call);
    },
  };

  verbs(): string[] {
    return this.calls.map((call) => call.verb);
  }

  /** The texts enqueued so far, oldest first. */
  enqueued(): string[] {
    return this.calls.filter((call) => call.verb === "enqueue").map((call) => call.payload.text as string);
  }

  emit(event: string, data: Record<string, unknown> = {}, sourceId = SOURCE): void {
    this.channel.handleEvent({ event, source_id: sourceId, data } satisfies SpeakdEvent);
  }

  /** `started` for `text`, split into sentences at each full stop, with `prefix` spoken before it. */
  started(text: string, prefix = "", sourceId = SOURCE): void {
    const spoken = prefix + text;
    const segments = [...spoken.matchAll(/[^.]+\./g)].map((match, index) => {
      const start = match.index + (match[0].length - match[0].trimStart().length);
      return { index, text: match[0].trim(), span_start: start, span_end: match.index + match[0].length };
    });
    this.emit("started", { text: spoken, segments }, sourceId);
  }

  position(index: number, sourceId = SOURCE): void {
    this.emit("position", { index }, sourceId);
  }

  finished(cancelled = false, sourceId = SOURCE): void {
    this.emit("finished", { cancelled, aborted: false }, sourceId);
  }
}

// Seven one-sentence segments: with the first section two segments long and
// the rest at most 40 characters, the sections are [0,1] [2,3] [4,5] [6].
const SEGMENTS = [
  "Zero is first.",
  "One follows.",
  "Two starts a page.",
  "Three is here.",
  "Four comes next.",
  "Five is late.",
  "Six ends it.",
].map((text) => ({ text }));

function setup() {
  const daemon = new FakeDaemon();
  const channel = new Channel({ client: daemon.client, sourceId: SOURCE, label: "A Paper", size: 40 });
  daemon.channel = channel;
  const highlights: (number | null)[] = [];
  const problems: Problem[] = [];
  channel.onHighlight((index) => highlights.push(index));
  channel.onProblem((problem) => problems.push(problem));
  return { daemon, channel, highlights, problems };
}

describe("Channel", () => {
  it("labels the channel and enqueues the first section with the pdf profile", async () => {
    const { daemon, channel } = setup();
    await channel.start(SEGMENTS, 0);
    expect(daemon.calls).toEqual([
      { verb: "set_label", sourceId: SOURCE, payload: { label: "A Paper" } },
      { verb: "enqueue", sourceId: SOURCE, payload: { text: "Zero is first. One follows.", profile: "pdf" } },
    ]);
  });

  it("enqueues the next section as soon as one starts, one ahead, and highlights as positions come", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    expect(daemon.enqueued()).toEqual(["Zero is first. One follows.", "Two starts a page. Three is here."]);

    daemon.position(0);
    daemon.position(1);
    expect(highlights).toEqual([0, 1]);
    expect(channel.speaking).toBe(true);
  });

  it("rolls from the end of one section into the next, and clears the highlight at the end", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    for (let section = 0; section < 4; section++) {
      daemon.started(daemon.enqueued()[section]!);
      await channel.idle();
      daemon.position(0);
      daemon.position(1);
      daemon.finished();
    }
    expect(daemon.enqueued()).toHaveLength(4);
    // Section four is one segment, split by the daemon into one sentence;
    // its second position names no sentence, so the highlight goes, and the
    // end of the read leaves it gone.
    expect(highlights).toEqual([0, 1, 2, 3, 4, 5, 6, null]);
    expect(channel.reading).toBe(false);
    expect(channel.speaking).toBe(false);
  });

  it("says when a read comes to its end, and only then", async () => {
    const { daemon, channel } = setup();
    const ends: number[] = [];
    channel.onEnd(() => ends.push(channel.generation));
    await channel.start(SEGMENTS, 5);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(0);
    expect(ends).toEqual([]);
    daemon.finished();
    expect(ends).toEqual([channel.generation]);

    // A stop is not an end: nobody reached the last sentence.
    await channel.start(SEGMENTS, 5);
    daemon.started(daemon.enqueued()[1]!);
    await channel.stop();
    daemon.finished(true);
    expect(ends).toHaveLength(1);
  });

  it("maps positions for a read started partway through the document", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 5);
    expect(daemon.enqueued()).toEqual(["Five is late. Six ends it."]);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(1);
    expect(highlights).toEqual([6]);
  });

  it("allows for the daemon speaking the channel's label first", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!, "A Paper: ");
    daemon.position(0);
    daemon.position(1);
    daemon.position(2);
    // The label and the first sentence are one daemon sentence here.
    expect(highlights).toEqual([0, 1, null]);
  });

  it("supersedes the old read on a jump, and ignores its late events", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    const first = daemon.enqueued()[0]!;
    daemon.started(first);
    await channel.idle();
    daemon.position(0);
    const generation = channel.generation;

    await channel.start(SEGMENTS, 4);
    expect(channel.generation).toBe(generation + 1);
    expect(daemon.verbs().slice(-3)).toEqual(["hush", "set_label", "enqueue"]);
    expect(daemon.calls.find((call) => call.verb === "hush")!.sourceId).toBe(SOURCE);

    // What the daemon was already saying about the old read.
    daemon.position(1);
    daemon.finished(true);
    // A late `started` of the old read's second section, dequeued just
    // before the hush landed.
    daemon.started(daemon.enqueued()[1]!);
    daemon.position(0);
    expect(highlights).toEqual([0, null]);

    daemon.started(daemon.enqueued().at(-1)!);
    daemon.position(0);
    expect(highlights).toEqual([0, null, 4]);
  });

  it("drops a start superseded before it got going, and enqueues only the newer one", async () => {
    const { daemon, channel } = setup();
    const one = channel.start(SEGMENTS, 0);
    const two = channel.start(SEGMENTS, 4);
    await Promise.all([one, two]);
    expect(daemon.verbs()).toEqual(["set_label", "enqueue"]);
    expect(daemon.enqueued()).toEqual(["Four comes next. Five is late."]);
  });

  it("drops a start superseded while its label was on the way", async () => {
    const { daemon, channel } = setup();
    const one = channel.start(SEGMENTS, 0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(daemon.verbs()).toEqual(["set_label"]);
    const two = channel.start(SEGMENTS, 4);
    await Promise.all([one, two]);
    expect(daemon.enqueued()).toEqual(["Four comes next. Five is late."]);
  });

  it("keeps a jump's hush behind an enqueue already on its way", async () => {
    const { daemon, channel } = setup();
    const one = channel.start(SEGMENTS, 0);
    // Until the enqueue has been sent, but not answered.
    while (!daemon.verbs().includes("enqueue")) await new Promise((resolve) => setTimeout(resolve, 0));
    const two = channel.start(SEGMENTS, 4);
    await Promise.all([one, two]);
    // The first read's section is queued in the daemon by now, so the jump
    // hushes it -- after its enqueue, never before, or it would be spoken
    // ahead of the section the user jumped to.
    expect(daemon.verbs()).toEqual(["set_label", "enqueue", "hush", "set_label", "enqueue"]);
  });

  it("stops with one hush on its own channel, clears the highlight and ignores what follows", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    daemon.position(0);

    await channel.stop();
    await channel.stop();
    expect(daemon.calls.filter((call) => call.verb === "hush")).toEqual([
      { verb: "hush", sourceId: SOURCE, payload: {} },
    ]);
    expect(highlights).toEqual([0, null]);

    daemon.position(1);
    daemon.finished(true);
    daemon.started(daemon.enqueued()[1]!);
    daemon.position(0);
    expect(highlights).toEqual([0, null]);
    expect(channel.reading).toBe(false);
  });

  it("remembers the last segment highlighted, for play after stop", async () => {
    const { daemon, channel } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(1);
    await channel.stop();
    expect(channel.lastIndex).toBe(1);
  });

  it("clears the highlight when something else cuts its sentence short", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(0);
    daemon.finished(true);
    expect(highlights).toEqual([0, null]);
  });

  it("keeps the highlight going when its own skip ends a section", async () => {
    const { daemon, channel, highlights } = setup();
    daemon.answer = (call) =>
      call.verb === "seek"
        ? { kind: "ok", data: { index: null, ended: true } }
        : { kind: "ok", data: { spoken: true } };
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    daemon.position(1);
    await channel.skip(1);
    daemon.finished(true);
    daemon.started(daemon.enqueued()[1]!);
    daemon.position(0);
    expect(highlights).toEqual([1, 2]);
  });

  it("speaks a section uncleaned when its cleanup throws", async () => {
    const daemon = new FakeDaemon();
    const channel = new Channel({
      client: daemon.client,
      sourceId: SOURCE,
      label: "A Paper",
      clean: () => {
        throw new Error("the endpoint is down");
      },
    });
    daemon.channel = channel;
    await channel.start([{ text: "A hyphen- ated word." }], 0);
    expect(daemon.enqueued()).toEqual(["A hyphen- ated word."]);
  });

  it("skips, pauses and resumes only while its own channel is speaking", async () => {
    const { daemon, channel } = setup();
    await channel.start(SEGMENTS, 0);
    expect((await channel.skip(1)).kind).toBe("refused");

    daemon.started("Something an agent says.", "", "claude:session");
    expect(channel.speakingChannel).toBe("claude:session");
    expect((await channel.pause()).kind).toBe("refused");
    daemon.finished(false, "claude:session");

    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    const before = daemon.calls.length;
    expect((await channel.skip(-1)).kind).toBe("ok");
    expect((await channel.skip(1)).kind).toBe("ok");
    expect((await channel.pause()).kind).toBe("ok");
    daemon.emit("transport", { paused: true }, "");
    expect(channel.paused).toBe(true);
    expect((await channel.resume()).kind).toBe("ok");
    expect(daemon.calls.slice(before)).toEqual([
      { verb: "seek", sourceId: SOURCE, payload: { by: -1 } },
      { verb: "seek", sourceId: SOURCE, payload: { by: 1 } },
      { verb: "pause", sourceId: SOURCE, payload: {} },
      { verb: "resume", sourceId: SOURCE, payload: {} },
    ]);
  });

  it("says so when the daemon cannot be reached, and gives the read up", async () => {
    const { daemon, channel, problems } = setup();
    daemon.answer = () => ({ kind: "bad-token", error: "missing or wrong token" });
    await channel.start(SEGMENTS, 0);
    expect(problems).toEqual([{ kind: "bad-token", error: "missing or wrong token" }]);
    expect(daemon.verbs()).toEqual(["set_label"]);
    expect(channel.reading).toBe(false);
  });

  it("recognises a section the daemon starts before it has answered the enqueue", async () => {
    // An idle daemon starts a job at once, and publishes `started` before
    // the enqueue's HTTP answer is on its way.
    const { daemon, channel, highlights } = setup();
    daemon.during = (call) => {
      if (call.verb === "enqueue" && daemon.enqueued().length === 1) daemon.started(call.payload.text as string);
    };
    await channel.start(SEGMENTS, 0);
    await channel.idle();
    daemon.position(1);
    expect(highlights).toEqual([1]);
    expect(daemon.enqueued()).toHaveLength(2);
  });

  it("forgets a section the daemon declined, so its text starts nothing", async () => {
    const { daemon, channel, highlights } = setup();
    daemon.answer = (call) =>
      call.verb === "enqueue" ? { kind: "ok", data: { spoken: false, reason: "muted" } } : { kind: "ok", data: {} };
    await channel.start(SEGMENTS, 0);
    daemon.started("Zero is first. One follows.");
    daemon.position(0);
    expect(highlights).toEqual([]);
  });

  it("says so when the daemon declines to speak the section", async () => {
    const { daemon, channel, problems } = setup();
    daemon.answer = (call) =>
      call.verb === "enqueue"
        ? { kind: "ok", data: { spoken: false, reason: "muted" } }
        : { kind: "ok", data: {} };
    await channel.start(SEGMENTS, 0);
    expect(problems).toEqual([{ kind: "declined", error: "muted" }]);
    expect(channel.reading).toBe(false);
  });

  it("cleans each section before it is enqueued", async () => {
    const { daemon, channel, highlights } = setup();
    await channel.start([{ text: "A hyphen- ated word." }, { text: "12 Next page." }], 0);
    expect(daemon.enqueued()).toEqual(["A hyphenated word. Next page."]);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(0);
    daemon.position(1);
    expect(highlights).toEqual([0, 1]);
  });

  it("does nothing for a start past the end of the document", async () => {
    const { daemon, channel } = setup();
    await channel.start(SEGMENTS, 7);
    expect(daemon.calls).toEqual([]);
    expect(channel.reading).toBe(false);
  });
});
