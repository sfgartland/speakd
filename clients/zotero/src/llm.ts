// Stage B: the optional LLM that repairs a section's residual OCR noise and
// classifies a section's borderline footnotes (proposal §5.2/§5.3, plan
// Tasks 10–11).
//
// Off until an endpoint is configured. Section 1 never calls it. The live
// path never waits on it: footnote classification is asked for in the
// background, cache-first, and a miss falls back to the policy default at
// once. Every text repair is diff-verified against a tightened budget, so a
// model that rewrote rather than repaired costs its own answer and nothing
// more. The deterministic cleanup is always the floor: what the model adds
// rides on top of it, and a failure returns exactly the deterministic stage.

import { cleanup, ocrSuspicion } from "./cleanup";
import { apply, compose, stageFromDiff, type Stage } from "./offsetmap";
import { GENERIC_PROFILE, type RequestProfile } from "./presets";
import type { FetchLike } from "./speakd";

/** The two repair prompts: a suspected scan's OCR noise, or the residual noise of a clean PDF. */
export type RepairMode = "ordinary" | "ocr";

/** What a borderline footnote is: a bare citation, or prose that carries content. */
export type FootnoteKind = "citation" | "substantive";

/** One footnote offered to the classifier, by its segment index. */
export interface FootnoteLabel {
  readonly index: number;
  readonly text: string;
}

/**
 * The cache-first footnote classifier the channel consults (Task 11):
 * `classify` is asked in the background for a section's residue and never
 * awaited; `cached` is the synchronous read a section due to enqueue makes.
 * `LlmClient` is one.
 */
export interface FootnoteClassifier {
  readonly configured: boolean;
  cached(text: string): FootnoteKind | undefined;
  classifyFootnotes(residue: readonly FootnoteLabel[]): Promise<Map<number, FootnoteKind>>;
}

/** The chat endpoint's configuration, from prefs.ts. Off until endpoint and model are set. */
export interface LlmConfig {
  readonly enabled: boolean;
  readonly endpoint: string;
  readonly apiKey: string;
  readonly model: string;
  /** Which opencode-only additions to send. Absent means the plain OpenAI
   * shape, so pre-preset callers and generic endpoints are unchanged. */
  readonly profile?: RequestProfile;
}

/**
 * The provisional OCR-suspicion gate (cleanup.ts `ocrSuspicion`): above it,
 * a section takes the OCR repair prompt; below, the ordinary one. Ships
 * conservative (high) and is conditional on a real scan fixture.
 */
export const DEFAULT_OCR_GATE = 6.0;

/** How many footnotes one classification call may carry (proposal §5.2). */
export const CLASSIFY_CHUNK = 20;

export interface LlmTimers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface LlmOptions {
  readonly config: LlmConfig;
  readonly fetch: FetchLike;
  /** How long one request may take before it is abandoned for the deterministic stage. */
  readonly timeoutMs?: number;
  readonly ocrGate?: number;
  readonly timers?: LlmTimers;
  readonly log?: (message: string) => void;
  /** The one bar mark a failed call leaves, called once however many fail. */
  readonly onFailure?: () => void;
  /** The opencode routing hint, one id per client; a random one by default. */
  readonly sessionId?: string;
}

const DEFAULT_TIMEOUT_MS = 8000;

/** The opencode routing hint. One id per client run is enough (gloss does the
 * same per plugin run); a random one keeps two clients apart. */
function defaultSessionId(): string {
  const crypto = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  return crypto?.randomUUID?.() ?? `speakd-${Date.now()}`;
}

/** A read that gives up after `timeoutMs`, resolving `null` instead. */
function withTimeout<T>(promise: Promise<T>, timeoutMs: number, timers: LlmTimers): Promise<T | null> {
  let handle: unknown;
  const timeout = new Promise<null>((resolve) => {
    handle = timers.setTimeout(() => resolve(null), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => timers.clearTimeout(handle));
}

/** The request this module would send. Pure, so the tests and the Test button
 * can read it, and the opencode additions are decided in one place. */
export function buildRequest(
  config: LlmConfig,
  sessionId: string,
  messages: { role: string; content: string }[],
): { url: string; headers: Record<string, string>; body: Record<string, unknown> } {
  const url = `${config.endpoint.replace(/\/+$/, "")}/chat/completions`;
  const profile = config.profile ?? GENERIC_PROFILE;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${config.apiKey}`,
    "Content-Type": "application/json",
  };
  // Both are opencode-only: OpenAI rejects unknown body fields with a 400.
  if (profile.sessionHeader) headers["x-opencode-session"] = sessionId;
  const body: Record<string, unknown> = {
    model: config.model,
    messages,
    temperature: 0,
  };
  if (profile.thinking) body["thinking"] = { type: "disabled" };
  return { url, headers, body };
}

/**
 * The plugin's chat client (proposal §4.2, §5.3): OpenAI-compatible
 * chat-completions, plus the session footnote-classification cache. Never
 * rejects; every failure is the deterministic stage and a log line.
 */
export class LlmClient {
  private readonly config: LlmConfig;
  private readonly fetch: FetchLike;
  private readonly timeoutMs: number;
  private readonly ocrGate: number;
  private readonly timers: LlmTimers;
  private readonly log: (message: string) => void;
  private readonly onFailure: () => void;
  private readonly sessionId: string;
  /** Footnote text to its kind, for the session: a re-read never re-asks. */
  private readonly cache = new Map<string, FootnoteKind>();
  private failed = false;

  constructor(options: LlmOptions) {
    this.config = options.config;
    this.fetch = options.fetch;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.ocrGate = options.ocrGate ?? DEFAULT_OCR_GATE;
    this.timers = options.timers ?? {
      setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
      clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    };
    this.log = options.log ?? (() => {});
    this.onFailure = options.onFailure ?? (() => {});
    this.sessionId = options.sessionId ?? defaultSessionId();
  }

  /** Whether the endpoint is configured at all: off until it is. */
  get configured(): boolean {
    return this.config.enabled && this.config.endpoint.trim() !== "" && this.config.model.trim() !== "";
  }

  /** The mode a section's own text calls for: the OCR prompt only past the gate. */
  modeFor(text: string): RepairMode {
    return ocrSuspicion(text) > this.ocrGate ? "ocr" : "ordinary";
  }

  /**
   * The section's deterministic cleanup, plus the model's repair when one is
   * configured and this is not the first section. Ordinal 0 is
   * byte-identical to the rules-only stage, so the first sound is as quick
   * as ever. A timeout, HTTP error, empty or unparsable answer, or an
   * over-budget rewrite leaves the deterministic stage standing.
   */
  async repairSection(text: string, mode: RepairMode, ordinal: number, signal?: AbortSignal): Promise<Stage> {
    const deterministic = cleanup(text);
    if (ordinal === 0 || !this.configured) return deterministic;
    const answer = await this.chat(repairPrompt(mode, deterministic.text), signal);
    if (answer === null) return deterministic;
    const budget = Math.max(8, Math.ceil(deterministic.text.length * 0.02));
    const repaired = stageFromDiff(deterministic.text, answer, budget);
    return apply(text, { text: repaired.text, map: compose(deterministic.map, repaired.map) });
  }

  /**
   * The kinds of a residue of borderline footnotes (proposal §5.2): one call
   * per chunk of at most 20, cache-first. An unresolved, unparsable or
   * short answer maps nothing for its chunk, so the caller's policy default
   * (kept) stands. Never rejects and never waits past its timeout.
   */
  async classifyFootnotes(residue: readonly FootnoteLabel[]): Promise<Map<number, FootnoteKind>> {
    const result = new Map<number, FootnoteKind>();
    const unknown: FootnoteLabel[] = [];
    for (const footnote of residue) {
      const cached = this.cache.get(footnote.text);
      if (cached === undefined) unknown.push(footnote);
      else result.set(footnote.index, cached);
    }
    for (let at = 0; at < unknown.length; at += CLASSIFY_CHUNK) {
      const chunk = unknown.slice(at, at + CLASSIFY_CHUNK);
      const answer = await this.chat(classifyPrompt(chunk));
      if (answer === null) continue;
      const labels = parseLabels(answer, chunk.length);
      if (labels === null) continue;
      chunk.forEach((footnote, k) => {
        const kind = labels[k]!;
        this.cache.set(footnote.text, kind);
        result.set(footnote.index, kind);
      });
    }
    return result;
  }

  /** The cached kind for a footnote's text, or undefined: the read's cache-first lookup. */
  cached(text: string): FootnoteKind | undefined {
    return this.cache.get(text);
  }

  /** One chat request, or null on any failure. Never rejects, never hangs past the timeout. */
  private async chat(prompt: string, signal?: AbortSignal): Promise<string | null> {
    const { url, headers, body } = buildRequest(this.config, this.sessionId, [{ role: "user", content: prompt }]);
    let response;
    try {
      response = await this.withTimeout(
        this.fetch(url, { method: "POST", headers, body: JSON.stringify(body), ...(signal === undefined ? {} : { signal }) }),
      );
      if (response === null) return this.give_up("the request timed out");
      if (response.status !== 200) return this.give_up(`HTTP ${response.status}`);
      const text = await this.withTimeout(response.text());
      if (text === null) return this.give_up("the answer timed out");
      const content = parseContent(text);
      if (content === null) return this.give_up("the answer was not a chat completion");
      if (content.trim() === "") return this.give_up("the model returned nothing");
      return content;
    } catch (error) {
      return this.give_up(String(error));
    }
  }

  /** The deterministic stage stands; the failure is logged and marked once. */
  private give_up(reason: string): null {
    this.log(`the model was not used: ${reason}`);
    if (!this.failed) {
      this.failed = true;
      this.onFailure();
    }
    return null;
  }

  private withTimeout<T>(promise: Promise<T>): Promise<T | null> {
    return withTimeout(promise, this.timeoutMs, this.timers);
  }
}

/** The Test button's verdict: a provider error, or how long the ping took. */
export interface TestResult {
  ok: boolean;
  ms: number;
  message: string;
}

export interface TestConnectionOptions {
  readonly config: LlmConfig;
  readonly fetch: FetchLike;
  readonly sessionId?: string;
  /** How long the ping may take before it is called off. */
  readonly timeoutMs?: number;
  readonly timers?: LlmTimers;
  /** Injectable clock, so the tests can read a deterministic time. */
  readonly now?: () => number;
}

const DEFAULT_TEST_TIMEOUT_MS = 15000;

/**
 * One minimal chat request, to tell whether the endpoint, key and model work
 * together. Never throws: every failure is a result, because the caller is a
 * preference pane's status line.
 */
export async function testConnection(options: TestConnectionOptions): Promise<TestResult> {
  const now = options.now ?? ((): number => Date.now());
  const started = now();
  const elapsed = (): number => Math.max(0, now() - started);
  const fail = (message: string): TestResult => ({ ok: false, ms: elapsed(), message });
  const pass = (): TestResult => ({ ok: true, ms: elapsed(), message: `connected in ${elapsed()} ms` });
  const timeoutMs = options.timeoutMs ?? DEFAULT_TEST_TIMEOUT_MS;
  const timers: LlmTimers = options.timers ?? {
    setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
    clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
  const sessionId = options.sessionId ?? defaultSessionId();
  try {
    const { url, headers, body } = buildRequest(options.config, sessionId, [{ role: "user", content: "ping" }]);
    body["max_tokens"] = 1;
    const response = await withTimeout(
      options.fetch(url, { method: "POST", headers, body: JSON.stringify(body) }),
      timeoutMs,
      timers,
    );
    if (response === null) return fail("the request timed out");
    if (response.status < 200 || response.status >= 300) {
      let detail = "";
      try {
        detail = await response.text();
      } catch {
        // The status alone is enough.
      }
      const trimmed = detail.trim().slice(0, 200);
      return fail(`${response.status}${trimmed ? `: ${trimmed}` : ""}`);
    }
    const text = await withTimeout(response.text(), timeoutMs, timers);
    if (text === null) return fail("the answer timed out");
    if (parseContent(text) === null) return fail("the endpoint did not answer a chat completion");
    return pass();
  } catch (error) {
    return fail(error instanceof Error ? error.message : String(error));
  }
}

/**
 * The client for the preferences as they stand now: `config` is read on
 * every call, so an endpoint changed or the model switched off takes effect
 * without a restart. The client (and its footnote cache) is kept while the
 * config is unchanged, and replaced when it is not.
 */
export function liveLlm(
  config: () => LlmConfig,
  make: (config: LlmConfig) => LlmClient,
): () => LlmClient {
  let key = "";
  let client: LlmClient | undefined;
  return () => {
    const now = config();
    const next = JSON.stringify(now);
    if (client === undefined || next !== key) {
      key = next;
      client = make(now);
    }
    return client;
  };
}

/** The section's deterministic stage plus the repair, as the `Clean` seam takes it. */
export function cleanWithLlm(client: LlmClient): (text: string, ordinal: number, signal?: AbortSignal) => Promise<Stage> {
  return (text, ordinal, signal) => client.repairSection(text, client.modeFor(text), ordinal, signal);
}

function repairPrompt(mode: RepairMode, text: string): string {
  const subject =
    mode === "ocr"
      ? "This text is an OCR layer from a scanned document and likely carries recognition errors."
      : "This text comes from a PDF's text layer and may carry residual noise.";
  return (
    `${subject} Clean it for a text-to-speech reader.\n` +
    "Fix only these classes of error, and nothing else: OCR confusion pairs whose intended word is " +
    "unambiguous (tbe -> the, modem -> modern, rn -> m, cl -> d), broken ligatures, split numbers " +
    "(1 994 -> 1994), and stray characters a scan left behind. Return the text unchanged if you are " +
    "unsure. Do not rephrase, do not reorder, do not add or remove sentences. Answer with the " +
    "corrected text alone, nothing else.\n\n" +
    `Text:\n${text}`
  );
}

function classifyPrompt(chunk: readonly FootnoteLabel[]): string {
  const numbered = chunk.map((footnote, k) => `${k + 1}. ${footnote.text}`).join("\n");
  return (
    "Below are footnotes from a philosophy paper, numbered. For each, decide: is it a bare citation " +
    "(references to works, page numbers, \"Ibid.\", \"Cf.\"), or does it contain substantive argument " +
    "or content? Answer a JSON array of the strings \"citation\" or \"substantive\", one per footnote, " +
    "in order, and nothing else.\n" +
    numbered
  );
}

/** The `content` of a chat completion, or null for anything else. */
function parseContent(text: string): string | null {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null) return null;
  const choices = (body as Record<string, unknown>).choices;
  if (!Array.isArray(choices) || choices.length === 0) return null;
  const first = choices[0];
  if (typeof first !== "object" || first === null) return null;
  const message = (first as Record<string, unknown>).message;
  if (typeof message !== "object" || message === null) return null;
  const content = (message as Record<string, unknown>).content;
  return typeof content === "string" ? content : null;
}

/** The answer as `count` labels in order, or null when it is not that. */
function parseLabels(answer: string, count: number): FootnoteKind[] | null {
  let body: unknown;
  try {
    body = JSON.parse(answer);
  } catch {
    return null;
  }
  if (!Array.isArray(body) || body.length !== count) return null;
  const labels: FootnoteKind[] = [];
  for (const label of body) {
    if (label !== "citation" && label !== "substantive") return null;
    labels.push(label);
  }
  return labels;
}
