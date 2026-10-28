import { describe, expect, it } from "vitest";
import { Channel, type Problem } from "../src/channel";
import { cleanup } from "../src/cleanup";
import type { LabelCategory } from "../src/labelstore";
import type { FootnotePolicy } from "../src/footnotes";
import { cleanWithLlm, LlmClient, type FootnoteClassifier, type FootnoteKind, type FootnoteLabel, type LlmTimers } from "../src/llm";
import type { CallResult, FetchLike, SpeakdEvent } from "../src/speakd";

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

// Six pages of a paper with a running head, a citation footnote and a prose
// footnote on every page, as the filters see them: rect y running bottom-up
// (footnotes low, body high).
const PAPER = [0, 1, 2, 3, 4, 5].flatMap((n) => [
  { text: "Sample Journal", anchor: "paragraphStart" as const, pageIndex: n, firstRectY: 770 },
  { text: `The body of page ${n} opens.`, anchor: "paragraphStart" as const, pageIndex: n, firstRectY: 740 },
  { text: `The body of page ${n} goes on.`, anchor: null, pageIndex: n, firstRectY: 726 },
  { text: "1 Kant, KrV, B25.", anchor: "paragraphStart" as const, pageIndex: n, firstRectY: 140 },
  {
    text: `2 A footnote of page ${n} holds real prose, and it argues its case across the sentences at length.`,
    anchor: "paragraphStart" as const,
    pageIndex: n,
    firstRectY: 120,
  },
]);

function setupPaper(footnotes: FootnotePolicy = "classify") {
  const daemon = new FakeDaemon();
  const channel = new Channel({ client: daemon.client, sourceId: SOURCE, label: "A Paper", size: 60, footnotes });
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

  it("still starts with speech when the first page's every segment is dropped", async () => {
    const { daemon, channel, highlights } = setupPaper();
    // Page 0 holds only a citation footnote, dropped by the plan; the body
    // starts on page 1.
    const segments = [
      { text: "1 Kant, KrV, B25.", anchor: "paragraphStart" as const, pageIndex: 0, firstRectY: 140 },
      ...PAPER.slice(5),
    ];
    await channel.start(segments, 0);
    // The dropped citation never appears; the first section is the next
    // body page's, and speech starts with it as ever. (The footnote's bare
    // marker digit is the page-number rule's to drop, as ever.)
    expect(daemon.enqueued()).toEqual([
      "The body of page 1 opens. The body of page 1 goes on. A footnote of page 1 holds real prose, " +
        "and it argues its case across the sentences at length.",
    ]);
    daemon.started(daemon.enqueued()[0]!);
    daemon.position(0);
    daemon.position(2);
    expect(highlights).toEqual([2, 5]);
  });

  it("reads without running heads and citation footnotes, prose deferred to its section's end", async () => {
    const { daemon, channel, highlights } = setupPaper();
    await channel.start(PAPER, 0);
    // The prose footnote of page 0 rides at the end of the section that owns
    // page 0, after the page's body, never inline. (Its bare marker digit is
    // the page-number rule's to drop, as ever.)
    expect(daemon.enqueued()).toEqual([
      "The body of page 0 opens. The body of page 0 goes on. A footnote of page 0 holds real " +
        "prose, and it argues its case across the sentences at length.",
    ]);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    // Every section of the read, not just the first: the head and the
    // citations are gone from all of them.
    expect(daemon.enqueued().length).toBeGreaterThan(1);
    for (const text of daemon.enqueued()) {
      expect(text).not.toContain("Sample Journal");
      expect(text).not.toContain("Kant, KrV");
    }
    // Highlights still resolve, by original document index: the deferred
    // footnote is segment 4, never a body segment.
    daemon.position(0);
    daemon.position(2);
    expect(highlights).toEqual([1, 4]);
  });

  it("changes exactly the footnotes' treatment when the policy pref flips", async () => {
    const deferAll = setupPaper("defer_all");
    await deferAll.channel.start(PAPER, 0);
    const first = deferAll.daemon.enqueued()[0]!;
    // The citation is kept now too, and read at the section's end, in order.
    // (Its bare marker digit is the page-number rule's to drop, as ever.)
    expect(first).toContain("Kant, KrV, B25.");
    expect(first.endsWith("at length.")).toBe(true);

    const dropAll = setupPaper("drop_all");
    await dropAll.channel.start(PAPER, 0);
    for (const text of dropAll.daemon.enqueued()) {
      expect(text).not.toContain("Kant, KrV");
      expect(text).not.toContain("holds real prose");
    }
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

  it("gives its read up when the stream is lost, forgets who was speaking, and still hushes on the next start", async () => {
    const { daemon, channel, problems } = setup();
    await channel.start(SEGMENTS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    expect(channel.speaking).toBe(true);

    // The daemon restarts: its `finished` for this section is never heard.
    channel.streamLost();
    expect(channel.speaking).toBe(false);
    expect(channel.speakingChannel).toBe("");
    expect(channel.reading).toBe(false);
    expect(problems).toEqual([{ kind: "no-daemon", error: "the connection to speakd was lost" }]);

    // An agent speaks now: nothing global may be sent on this reader's behalf.
    daemon.started("Something an agent says.", "", "claude:session");
    expect((await channel.skip(1)).kind).toBe("refused");
    expect((await channel.pause()).kind).toBe("refused");

    // What the old daemon may still hold of this channel's is hushed first.
    const before = daemon.calls.length;
    await channel.start(SEGMENTS, 0);
    expect(daemon.verbs().slice(before)).toEqual(["hush", "set_label", "enqueue"]);
  });

  it("says nothing when the stream is lost with no read under way", () => {
    const { channel, problems } = setup();
    channel.streamLost();
    expect(problems).toEqual([]);
  });

  it("hushes its own channel on an explicit stop, even with nothing of its own known queued", async () => {
    const { daemon, channel } = setup();
    // Speaking on this channel, from before the plugin knew of it: another
    // Zotero session's read, or one given up when the stream was lost.
    daemon.started("A read nobody here started.");
    await channel.stop({ always: true });
    expect(daemon.calls).toEqual([{ verb: "hush", sourceId: SOURCE, payload: {} }]);
  });

  it("sends no hush for a plain stop with nothing of its own queued, as a tab closing is", async () => {
    const { daemon, channel } = setup();
    await channel.stop();
    expect(daemon.calls).toEqual([]);
  });

  it("says a document with no text has none to read, and sends nothing", async () => {
    const { daemon, channel, problems } = setup();
    await channel.start([{ text: "" }, { text: "  " }], 0);
    expect(problems).toEqual([{ kind: "empty", error: "this document has no text to read" }]);
    expect(daemon.calls).toEqual([]);
    expect(channel.reading).toBe(false);
  });
});

// A classifier whose cache the test pre-seeds, so the channel's cache-first
// resolution can be driven without a network; `hang` models a call that
// never answers.
class FakeClassifier implements FootnoteClassifier {
  readonly configured = true;
  readonly calls: FootnoteLabel[][] = [];
  constructor(
    private readonly kinds = new Map<string, FootnoteKind>(),
    private readonly hang = false,
  ) {}
  cached(text: string): FootnoteKind | undefined {
    return this.kinds.get(text);
  }
  async classifyFootnotes(residue: readonly FootnoteLabel[]): Promise<Map<number, FootnoteKind>> {
    this.calls.push([...residue]);
    if (this.hang) return new Promise(() => {});
    const out = new Map<number, FootnoteKind>();
    for (const footnote of residue) {
      const kind = this.kinds.get(footnote.text);
      if (kind !== undefined) out.set(footnote.index, kind);
    }
    return out;
  }
}

// A stubbed chat endpoint: each answer is a completion holding `content`, or
// "hang"; what was asked is recorded.
function stubLlm(answers: (string | "hang")[]) {
  const requests: { body?: string }[] = [];
  const fetch: FetchLike = async (_url, init) => {
    requests.push({ body: init?.body });
    const answer = answers.shift();
    if (answer === undefined || answer === "hang") return new Promise(() => {});
    return new Response(JSON.stringify({ choices: [{ message: { content: answer } }] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return { fetch, requests };
}

const PROSE_NOTE =
  "2 A footnote of page 0 holds real prose, and it argues its case across the sentences at length.";

describe("Channel with a footnote classifier", () => {
  it("drops a cache-resolved citation footnote, and the body still reads", async () => {
    const classifier = new FakeClassifier(new Map([[PROSE_NOTE, "citation"]]));
    const daemon = new FakeDaemon();
    const channel = new Channel({
      client: daemon.client,
      sourceId: SOURCE,
      label: "A Paper",
      size: 60,
      footnotes: "classify",
      footnoteClassifier: classifier,
    });
    daemon.channel = channel;
    await channel.start(PAPER, 0);
    const first = daemon.enqueued()[0]!;
    expect(first).not.toContain("holds real prose");
    expect(first).toContain("The body of page 0 opens.");
  });

  it("never blocks enqueueNext on a classification that hangs", async () => {
    const classifier = new FakeClassifier(new Map(), true);
    const daemon = new FakeDaemon();
    const channel = new Channel({
      client: daemon.client,
      sourceId: SOURCE,
      label: "A Paper",
      size: 60,
      footnotes: "classify",
      footnoteClassifier: classifier,
    });
    daemon.channel = channel;
    await channel.start(PAPER, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    expect(daemon.enqueued().length).toBeGreaterThan(1);
  });

  it("makes zero classification calls under drop_all and defer_all", async () => {
    for (const policy of ["drop_all", "defer_all"] as const) {
      const classifier = new FakeClassifier(new Map(), true);
      const daemon = new FakeDaemon();
      const channel = new Channel({
        client: daemon.client,
        sourceId: SOURCE,
        label: "A Paper",
        size: 60,
        footnotes: policy,
        footnoteClassifier: classifier,
      });
      daemon.channel = channel;
      await channel.start(PAPER, 0);
      await channel.idle();
      daemon.started(daemon.enqueued()[0]!);
      await channel.idle();
      expect(classifier.calls).toEqual([]);
    }
  });
});

// Three one-sentence sections ([0], [1], [2]) with a typo in the second, for
// the LLM repair stage.
const SECTIONS = [
  { text: "First section text." },
  { text: "Second section has a tbe typo." },
  { text: "Third section is here." },
];

const instantTimers = {
  setTimeout: (callback: () => void) => {
    queueMicrotask(callback);
    return 0;
  },
  clearTimeout: () => {},
};
const neverTimers = { setTimeout: () => 0, clearTimeout: () => {} };

function llmChannel(answers: (string | "hang")[], timers: LlmTimers, enabled = true) {
  const { fetch, requests } = stubLlm(answers);
  const client = new LlmClient({
    config: { enabled, endpoint: enabled ? "https://e.test/v1" : "", apiKey: "k", model: enabled ? "m" : "" },
    fetch,
    timers,
    log: () => {},
  });
  const daemon = new FakeDaemon();
  const channel = new Channel({
    client: daemon.client,
    sourceId: SOURCE,
    label: "A Paper",
    firstSize: 1,
    size: 30,
    clean: cleanWithLlm(client),
  });
  daemon.channel = channel;
  return { daemon, channel, requests };
}

describe("Channel with the Stage B clean", () => {
  it("skips the LLM for ordinal 0: zero fetches and the deterministic text", async () => {
    const { daemon, channel, requests } = llmChannel(["changed"], neverTimers);
    await channel.start(SECTIONS, 0);
    expect(requests).toHaveLength(0);
    expect(daemon.enqueued()[0]).toBe(cleanup(SECTIONS[0]!.text).text);
  });

  it("repairs sections after the first with a configured endpoint", async () => {
    const { daemon, channel, requests } = llmChannel(["Second section has a the typo."], neverTimers);
    await channel.start(SECTIONS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    expect(requests).toHaveLength(1);
    expect(daemon.enqueued()[1]).toBe("Second section has a the typo.");
  });

  it("leaves speech identical to rules-only when the model fails", async () => {
    const { daemon, channel } = llmChannel(["hang"], instantTimers);
    await channel.start(SECTIONS, 0);
    daemon.started(daemon.enqueued()[0]!);
    await channel.idle();
    expect(daemon.enqueued()[1]).toBe(cleanup(SECTIONS[1]!.text).text);
  });

  it("hushes at once on stop while a repair is still in flight", async () => {
    const { daemon, channel } = llmChannel(["hang"], neverTimers);
    await channel.start(SECTIONS, 0);
    daemon.started(daemon.enqueued()[0]!);
    // The second section's repair now waits on a fetch that never answers.
    await new Promise((resolve) => setTimeout(resolve, 5));
    const before = daemon.verbs().length;
    await Promise.race([channel.stop(), new Promise((_, reject) => setTimeout(() => reject(new Error("stop waited on the LLM")), 500))]);
    expect(daemon.verbs().slice(before)).toContain("hush");
    expect(daemon.enqueued()).toHaveLength(1);
  });

  it("is indistinguishable from Stage A when unconfigured", async () => {
    const configured = llmChannel([], neverTimers);
    const unconfigured = llmChannel([], neverTimers, false);
    await configured.channel.start(SECTIONS, 0);
    await unconfigured.channel.start(SECTIONS, 0);
    expect(configured.requests).toHaveLength(0);
    expect(unconfigured.requests).toHaveLength(0);
    expect(unconfigured.daemon.enqueued()).toEqual(configured.daemon.enqueued());
  });
});

// ---- labels (Task 3) ----

function labelChannel(
  overrides: ((segments: readonly { text: string }[]) => ReadonlyMap<number, LabelCategory> | null) | undefined,
  footnotes: FootnotePolicy = "classify",
  size = 1000,
) {
  const daemon = new FakeDaemon();
  const logs: string[] = [];
  const channel = new Channel({
    client: daemon.client,
    sourceId: SOURCE,
    label: "A Paper",
    size,
    footnotes,
    overrides,
    log: (message) => logs.push(message),
  });
  daemon.channel = channel;
  return { daemon, channel, logs };
}

function labelled(text: string, anchor: string, pageIndex: number, firstRectY: number) {
  return { text, anchor, pageIndex, firstRectY };
}

describe("Channel with a label override map", () => {
  it("never enqueues a labelled-away segment, and audits the label once", async () => {
    const segments = [
      labelled("Body one is here.", "paragraphStart", 0, 770),
      labelled("A segment to drop.", "paragraphStart", 0, 740),
      labelled("Body two follows.", "paragraphStart", 0, 720),
    ];
    const { daemon, channel, logs } = labelChannel(() => new Map([[1, "running-head"]]));
    await channel.start(segments, 0);
    expect(daemon.enqueued()[0]).toContain("Body one is here.");
    expect(daemon.enqueued()[0]).not.toContain("A segment to drop.");
    expect(logs).toContain("label: segment 1 → running-head (dropped)");
  });

  it("restores a head-filtered false positive with a body label", async () => {
    const segments = [];
    for (let n = 0; n < 6; n++) {
      segments.push(labelled("Sample Journal", "paragraphStart", n, 770));
      segments.push(
        labelled(
          `Body of page ${n} opens with a paragraph long enough that the recurrence filter never mistakes it for a head.`,
          "paragraphStart",
          n,
          740,
        ),
      );
    }
    const unaided = labelChannel(undefined);
    await unaided.channel.start(segments, 0);
    expect(unaided.daemon.enqueued()[0]).not.toContain("Sample Journal");
    const labelledChannel = labelChannel(() => new Map([[0, "body"]]));
    await labelledChannel.channel.start(segments, 0);
    expect(labelledChannel.daemon.enqueued()[0]).toContain("Sample Journal");
  });

  it("lands a labelled footnote at its section's end under defer_all", async () => {
    const segments = [
      labelled("Body one is here.", "paragraphStart", 0, 770),
      labelled("Target to defer.", "paragraphStart", 0, 740),
      labelled("Body two follows.", "paragraphStart", 0, 720),
    ];
    const { daemon, channel } = labelChannel(() => new Map([[1, "footnote"]]), "defer_all");
    await channel.start(segments, 0);
    expect(daemon.enqueued()[0]).toBe("Body one is here. Body two follows. Target to defer.");
  });

  it("calls the resolver exactly once per start", async () => {
    let calls = 0;
    const resolver = () => {
      calls++;
      return new Map<number, LabelCategory>();
    };
    const { channel } = labelChannel(resolver);
    await channel.start(SEGMENTS, 0);
    await channel.start(SEGMENTS, 0);
    expect(calls).toBe(2);
  });

  it("reads as unlabelled when the resolver returns null", async () => {
    const withNull = labelChannel(() => null);
    const without = labelChannel(undefined);
    await withNull.channel.start(SEGMENTS, 0);
    await without.channel.start(SEGMENTS, 0);
    expect(withNull.daemon.enqueued()).toEqual(without.daemon.enqueued());
  });

  it("fails open when the resolver throws", async () => {
    const without = labelChannel(undefined);
    const throwing = labelChannel(() => {
      throw new Error("bad file");
    });
    await without.channel.start(SEGMENTS, 0);
    await throwing.channel.start(SEGMENTS, 0);
    expect(throwing.daemon.enqueued()).toEqual(without.daemon.enqueued());
  });
});
