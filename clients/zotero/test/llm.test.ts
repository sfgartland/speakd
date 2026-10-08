import { describe, expect, it } from "vitest";
import { cleanup, ocrSuspicion } from "../src/cleanup";
import { verify } from "../src/offsetmap";
import type { FetchLike } from "../src/speakd";
import { LlmClient, type LlmConfig, type LlmTimers } from "../src/llm";

const CONFIG: LlmConfig = { enabled: true, endpoint: "https://example.test/v1", apiKey: "sk-x", model: "test-model" };

interface Request {
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: string;
}

// A stubbed fetch answering each request with the next answer: a chat
// completion holding `content`, an Error to reject with, a bare Response, or
// "hang" for a request that is never answered. Records what it was asked.
function stubFetch(answers: (string | Error | Response | "hang")[]) {
  const requests: Request[] = [];
  const fetch: FetchLike = async (url, init) => {
    requests.push({
      url,
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      body: init?.body,
    });
    const answer = answers.shift();
    if (answer === undefined || answer === "hang") return new Promise(() => {});
    if (answer instanceof Error) throw answer;
    if (typeof answer === "string") {
      return new Response(JSON.stringify({ choices: [{ message: { content: answer } }] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return answer;
  };
  return { fetch, requests };
}

// A timers whose timeout never fires, so a stubbed fetch always decides the
// race; the timeout tests below pass `instantTimers` instead.
const neverTimers = {
  setTimeout: () => 0,
  clearTimeout: () => {},
};

// A timers whose timeout fires at once, so a hung request races to its
// deadline without a real wait.
const instantTimers = {
  setTimeout: (callback: () => void) => {
    queueMicrotask(callback);
    return 0;
  },
  clearTimeout: () => {},
};

function llm(
  answers: (string | Error | Response | "hang")[],
  overrides: Partial<LlmConfig> = {},
  options: { timers?: LlmTimers; onFailure?: () => void; log?: (message: string) => void } = {},
) {
  const { fetch, requests } = stubFetch(answers);
  const client = new LlmClient({
    config: { ...CONFIG, ...overrides },
    fetch,
    timeoutMs: 5,
    timers: options.timers ?? neverTimers,
    log: options.log ?? (() => {}),
    onFailure: options.onFailure,
  });
  return { client, requests };
}

const lastPrompt = (request: Request | undefined): string => {
  const body = JSON.parse(request?.body ?? "{}") as { messages?: { content?: string }[] };
  return body.messages?.[0]?.content ?? "";
};

describe("LlmClient.repairSection", () => {
  it("sends the section with the edit contract, and returns a verified Stage", async () => {
    const { client, requests } = llm(["The modern reader was repaired."]);
    const input = "The modem reader was repaired.";
    const stage = await client.repairSection(input, "ordinary", 1);
    expect(requests).toHaveLength(1);
    expect(requests[0]!.url).toBe("https://example.test/v1/chat/completions");
    expect(requests[0]!.headers.Authorization).toBe("Bearer sk-x");
    const prompt = lastPrompt(requests[0]);
    expect(prompt).toContain(input);
    expect(prompt).toContain("unchanged");
    expect(stage.text).toBe("The modern reader was repaired.");
    expect(verify(stage.map, input.length, stage.text.length)).toBe(true);
  });

  it("falls back to the deterministic stage on a timeout, without waiting", async () => {
    const { client } = llm(["hang"], {}, { timers: instantTimers });
    const input = "The tbe modem. And 1 994.";
    const stage = await client.repairSection(input, "ordinary", 1);
    expect(stage.text).toBe(cleanup(input).text);
  });

  it("falls back on an HTTP error", async () => {
    const { client } = llm([new Response("boom", { status: 500 })]);
    const input = "The tbe modem.";
    expect((await client.repairSection(input, "ordinary", 1)).text).toBe(cleanup(input).text);
  });

  it("falls back on an empty answer", async () => {
    const { client } = llm([""]);
    const input = "The tbe modem.";
    expect((await client.repairSection(input, "ordinary", 1)).text).toBe(cleanup(input).text);
  });

  it("rejects an over-budget rewrite: the deterministic text stands", async () => {
    const input = "a".repeat(4000);
    const rewrite = "b".repeat(4000);
    const { client } = llm([rewrite]);
    const stage = await client.repairSection(input, "ordinary", 1);
    // maxEdits = max(8, ceil(4000 * 0.02)) = 80; 4000 substitutions is a rewrite.
    expect(stage.text).toBe(cleanup(input).text);
  });

  it("marks one failure, once, and logs it", async () => {
    const marks: number[] = [];
    const logs: string[] = [];
    const { client } = llm(["hang"], {}, { timers: instantTimers, onFailure: () => marks.push(1), log: (m) => logs.push(m) });
    await client.repairSection("The tbe modem.", "ordinary", 1);
    await client.repairSection("The tbe modem.", "ordinary", 2);
    expect(marks).toHaveLength(1);
    expect(logs.length).toBeGreaterThanOrEqual(1);
  });

  it("short-circuits ordinal 0 before any fetch", async () => {
    const { client, requests } = llm(["anything"]);
    const input = "The tbe modem.";
    expect((await client.repairSection(input, "ordinary", 0)).text).toBe(cleanup(input).text);
    expect(requests).toHaveLength(0);
  });

  it("short-circuits an unconfigured endpoint before any fetch", async () => {
    for (const config of [
      { enabled: false },
      { endpoint: "" },
      { model: "" },
    ] as Partial<LlmConfig>[]) {
      const { client, requests } = llm(["anything"], config);
      await client.repairSection("The tbe modem.", "ordinary", 1);
      expect(requests).toHaveLength(0);
      expect(client.configured).toBe(false);
    }
  });

  it("picks the OCR prompt from the suspicion gate, not otherwise", async () => {
    const clean = "A clean sentence with nothing suspicious in it at all.";
    expect(ocrSuspicion(clean)).toBeLessThan(6);
    const mangled = "Tbe mo\u00adde rn scan says 1 994 and 2 001 and \u00b7 tbe year \u00ac of tbe.";
    const { client, requests } = llm(["A clean sentence with nothing suspicious in it at all.", "Tbe modem scan."]);
    expect(client.modeFor(clean)).toBe("ordinary");
    expect(client.modeFor(mangled)).toBe("ocr");
    await client.repairSection(clean, "ordinary", 1);
    expect(lastPrompt(requests[0])).not.toContain("scanned document");
    await client.repairSection(mangled, client.modeFor(mangled), 1);
    expect(lastPrompt(requests[1])).toContain("scanned document");
  });
});

describe("LlmClient.classifyFootnotes", () => {
  it("classifies a residue in one call, both footnotes in the prompt and in order", async () => {
    const { client, requests } = llm([JSON.stringify(["citation", "substantive"])]);
    const kinds = await client.classifyFootnotes([
      { index: 1, text: "Kant, KrV, B25." },
      { index: 2, text: "The lecture reverses the order of asking." },
    ]);
    expect(requests).toHaveLength(1);
    const prompt = lastPrompt(requests[0]);
    expect(prompt).toContain("Kant, KrV, B25.");
    expect(prompt).toContain("The lecture reverses the order of asking.");
    expect(prompt).toContain("JSON");
    expect([...kinds.entries()]).toEqual([
      [1, "citation"],
      [2, "substantive"],
    ]);
  });

  it("chunks a 45-footnote residue into three calls of at most 20", async () => {
    const labels = (n: number) => JSON.stringify(Array.from({ length: n }, () => "substantive"));
    const { client, requests } = llm([labels(20), labels(20), labels(5)]);
    const residue = Array.from({ length: 45 }, (_, i) => ({ index: i, text: `footnote ${i}` }));
    const kinds = await client.classifyFootnotes(residue);
    expect(requests).toHaveLength(3);
    expect(kinds.size).toBe(45);
  });

  it("returns the cached kind for an identical footnote without a call", async () => {
    const { client, requests } = llm([JSON.stringify(["citation"])]);
    const first = await client.classifyFootnotes([{ index: 0, text: "Kant, KrV, B25." }]);
    const second = await client.classifyFootnotes([{ index: 7, text: "Kant, KrV, B25." }]);
    expect(requests).toHaveLength(1);
    expect(first.get(0)).toBe("citation");
    expect(second.get(7)).toBe("citation");
    expect(client.cached("Kant, KrV, B25.")).toBe("citation");
  });

  it("maps nothing and falls back when the answer is unparsable or the wrong length", async () => {
    for (const answer of ["not json", JSON.stringify(["citation"]), JSON.stringify(["citation", "citation", "citation"])]) {
      const { client, requests } = llm([answer]);
      const kinds = await client.classifyFootnotes([
        { index: 0, text: "Kant, KrV, B25." },
        { index: 1, text: "Another one, at length and with more words." },
      ]);
      expect(requests).toHaveLength(1);
      expect(kinds.size).toBe(0);
    }
  });

  it("makes no call for an empty residue", async () => {
    const { client, requests } = llm([]);
    expect((await client.classifyFootnotes([])).size).toBe(0);
    expect(requests).toHaveLength(0);
  });
});
