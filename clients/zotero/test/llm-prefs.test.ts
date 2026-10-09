// The preference pane's wiring, driven without a browser: speakd's client
// does not carry happy-dom, and the task forbids adding it, so this file
// supplies the small slice of DOM `wirePrefsPane` touches. The cases mirror
// gloss's test/prefs-ui.test.ts.
import { describe, expect, it, vi } from "vitest";

import { modelOptions, providerIdFor, readValues, wirePrefsPane, type PrefsPaneApi, type PrefsValues } from "../src/llm-prefs";
import {
  LLM_ENDPOINT_PREF,
  LLM_MODEL_PREF,
  LLM_PROVIDER_PREF,
} from "../src/prefs";

const XUL_NS = "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul";

class FakeElement {
  readonly children: FakeElement[] = [];
  readonly attrs = new Map<string, string>();
  readonly listeners = new Map<string, (() => void)[]>();
  value = "";
  hidden = false;
  disabled = false;
  checked = false;
  private text = "";

  constructor(
    readonly tagName: string,
    readonly namespaceURI: string | null = null,
  ) {}

  get textContent(): string {
    return this.text;
  }

  set textContent(value: string) {
    this.text = value;
    this.children.length = 0;
  }

  setAttribute(name: string, value: string): void {
    this.attrs.set(name, value);
  }

  getAttribute(name: string): string | undefined {
    return this.attrs.get(name);
  }

  appendChild(child: FakeElement): FakeElement {
    this.children.push(child);
    return child;
  }

  addEventListener(type: string, listener: () => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  dispatchEvent(event: { type: string }): boolean {
    for (const listener of this.listeners.get(event.type) ?? []) listener();
    return true;
  }

  focus(): void {}

  click(): void {
    this.dispatchEvent({ type: "click" });
  }
}

class FakeDocument {
  private readonly elements = new Map<string, FakeElement>();

  createXULElement(tagName: string): FakeElement {
    return new FakeElement(tagName, XUL_NS);
  }

  createElementNS(namespace: string, tagName: string): FakeElement {
    return new FakeElement(tagName, namespace);
  }

  getElementById(id: string): FakeElement | undefined {
    return this.elements.get(id);
  }

  register(id: string, tagName: string, namespace: string | null = null): FakeElement {
    const element = new FakeElement(tagName, namespace);
    this.elements.set(id, element);
    return element;
  }
}

interface Pane {
  doc: FakeDocument;
  win: { document: FakeDocument };
  el: (id: string) => FakeElement;
  menuValues: (id: string) => (string | undefined)[];
}

function mountPane(): Pane {
  const doc = new FakeDocument();
  doc.register("speakd-reader-llm-provider", "menulist", XUL_NS);
  doc.register("speakd-reader-llm-provider-menu", "menupopup", XUL_NS);
  doc.register("speakd-reader-llm-base-url", "input");
  doc.register("speakd-reader-llm-model", "menulist", XUL_NS);
  doc.register("speakd-reader-llm-model-menu", "menupopup", XUL_NS);
  doc.register("speakd-reader-llm-model-custom", "input");
  doc.register("speakd-reader-llm-key", "input");
  doc.register("speakd-reader-llm-test", "button");
  doc.register("speakd-reader-llm-test-status", "description");
  doc.register("speakd-reader-llm-custom-profile", "vbox", XUL_NS);
  doc.register("speakd-reader-llm-custom-session", "checkbox");
  doc.register("speakd-reader-llm-custom-thinking", "checkbox");
  const el = (id: string): FakeElement => doc.getElementById(id)!;
  return {
    doc,
    win: { document: doc },
    el,
    menuValues: (id) => el(id).children.map((item) => item.value),
  };
}

interface FakeApi {
  api: PrefsPaneApi;
  store: Record<string, string | boolean>;
  test: ReturnType<typeof vi.fn>;
}

function fakeApi(initial: Record<string, string | boolean> = {}): FakeApi {
  const store: Record<string, string | boolean> = { ...initial };
  const test = vi.fn(async () => ({ ok: true, ms: 5, message: "connected in 5 ms" }));
  return {
    store,
    test,
    api: {
      getPref: (key) => store[key] ?? "",
      setPref: (key, value) => {
        store[key] = value;
      },
      test,
    },
  };
}

describe("modelOptions", () => {
  it("lists the preset's models, then the custom entry", () => {
    const options = modelOptions(undefined);
    expect(options.map((o) => o.value)).toEqual(["custom"]);
  });
});

describe("providerIdFor", () => {
  it("prefers a chosen provider, else matches the URL, else custom", () => {
    expect(providerIdFor("https://example.com/v1", "openai")).toBe("openai");
    expect(providerIdFor("https://api.deepseek.com", "")).toBe("deepseek");
    expect(providerIdFor("https://example.com/v1", "")).toBe("custom");
  });
});

describe("wirePrefsPane", () => {
  it("selects the stored provider and fills the model picker on load", () => {
    const pane = mountPane();
    const { api } = fakeApi({
      [LLM_PROVIDER_PREF]: "deepseek",
      [LLM_ENDPOINT_PREF]: "https://api.deepseek.com",
      [LLM_MODEL_PREF]: "deepseek-flash",
    });
    pane.el("speakd-reader-llm-base-url").value = "https://api.deepseek.com";
    pane.el("speakd-reader-llm-model-custom").value = "deepseek-flash";

    wirePrefsPane(pane.win, api);

    expect(pane.el("speakd-reader-llm-provider").value).toBe("deepseek");
    expect(pane.menuValues("speakd-reader-llm-model-menu")).toEqual(["deepseek-flash", "deepseek-v4-pro", "custom"]);
  });

  it("creates menu items in the XUL namespace so Zotero renders them", () => {
    // The preference pane is a XUL document, and `menupopup` only renders
    // XUL-namespaced `menuitem` children. `document.createElement('menuitem')`
    // yields a null-namespace element, which shows up as an empty dropdown.
    const pane = mountPane();
    const { api } = fakeApi({
      [LLM_PROVIDER_PREF]: "deepseek",
      [LLM_ENDPOINT_PREF]: "https://api.deepseek.com",
      [LLM_MODEL_PREF]: "deepseek-flash",
    });
    pane.el("speakd-reader-llm-base-url").value = "https://api.deepseek.com";

    wirePrefsPane(pane.win, api);

    for (const id of ["speakd-reader-llm-provider-menu", "speakd-reader-llm-model-menu"]) {
      const items = pane.el(id).children;
      expect(items.length).toBeGreaterThan(0);
      for (const item of items) {
        expect(item.namespaceURI).toBe(XUL_NS);
      }
    }
  });

  it("changing the provider writes the base URL and a sensible model", () => {
    const pane = mountPane();
    const { api, store } = fakeApi({
      [LLM_PROVIDER_PREF]: "deepseek",
      [LLM_ENDPOINT_PREF]: "https://api.deepseek.com",
      [LLM_MODEL_PREF]: "deepseek-flash",
    });
    pane.el("speakd-reader-llm-base-url").value = "https://api.deepseek.com";
    pane.el("speakd-reader-llm-model-custom").value = "deepseek-flash";
    wirePrefsPane(pane.win, api);

    const provider = pane.el("speakd-reader-llm-provider");
    provider.value = "openai";
    provider.dispatchEvent({ type: "command" });

    expect(pane.el("speakd-reader-llm-base-url").value).toBe("https://api.openai.com/v1");
    expect(store[LLM_ENDPOINT_PREF]).toBe("https://api.openai.com/v1");
    expect(store[LLM_PROVIDER_PREF]).toBe("openai");
    expect(pane.el("speakd-reader-llm-model-custom").value).toBe("gpt-5.6-sol");
    expect(store[LLM_MODEL_PREF]).toBe("gpt-5.6-sol");
  });

  it("choosing Custom in the model picker reveals the free-text model field", () => {
    const pane = mountPane();
    const { api } = fakeApi({
      [LLM_PROVIDER_PREF]: "openai",
      [LLM_ENDPOINT_PREF]: "https://api.openai.com/v1",
      [LLM_MODEL_PREF]: "gpt-5",
    });
    pane.el("speakd-reader-llm-base-url").value = "https://api.openai.com/v1";
    pane.el("speakd-reader-llm-model-custom").value = "gpt-5";
    wirePrefsPane(pane.win, api);

    const picker = pane.el("speakd-reader-llm-model");
    picker.value = "custom";
    picker.dispatchEvent({ type: "command" });
    expect(pane.el("speakd-reader-llm-model-custom").hidden).toBe(false);
  });

  it("shows the custom request flags only for the custom provider", () => {
    const pane = mountPane();
    const { api } = fakeApi({ [LLM_PROVIDER_PREF]: "openai", [LLM_ENDPOINT_PREF]: "https://api.openai.com/v1" });
    pane.el("speakd-reader-llm-base-url").value = "https://api.openai.com/v1";
    wirePrefsPane(pane.win, api);
    expect(pane.el("speakd-reader-llm-custom-profile").hidden).toBe(true);

    const provider = pane.el("speakd-reader-llm-provider");
    provider.value = "custom";
    provider.dispatchEvent({ type: "command" });
    expect(pane.el("speakd-reader-llm-custom-profile").hidden).toBe(false);
  });

  it("Test sends the pane's current values and reports the result", async () => {
    const pane = mountPane();
    const { api, test } = fakeApi({
      [LLM_PROVIDER_PREF]: "openai",
      [LLM_ENDPOINT_PREF]: "https://api.openai.com/v1",
      [LLM_MODEL_PREF]: "gpt-5.6-sol",
    });
    pane.el("speakd-reader-llm-base-url").value = "https://api.openai.com/v1";
    pane.el("speakd-reader-llm-model-custom").value = "gpt-5.6-sol";
    wirePrefsPane(pane.win, api);

    pane.el("speakd-reader-llm-key").value = "sk-x";
    pane.el("speakd-reader-llm-test").click();

    await vi.waitFor(() => expect(test).toHaveBeenCalledTimes(1));
    const values = test.mock.calls[0]?.[0] as PrefsValues;
    expect(values).toMatchObject({ provider: "openai", baseUrl: "https://api.openai.com/v1", model: "gpt-5.6-sol" });
    expect(readValues(pane.win).apiKey).toBe("sk-x");
    await vi.waitFor(() => expect(pane.el("speakd-reader-llm-test-status").textContent).toMatch(/connected/));
  });

  it("Test shows a failure message too", async () => {
    const pane = mountPane();
    const { api, test } = fakeApi({ [LLM_PROVIDER_PREF]: "openai", [LLM_ENDPOINT_PREF]: "https://api.openai.com/v1" });
    test.mockResolvedValueOnce({ ok: false, ms: 3, message: "401: bad key" });
    pane.el("speakd-reader-llm-base-url").value = "https://api.openai.com/v1";
    wirePrefsPane(pane.win, api);

    pane.el("speakd-reader-llm-test").click();
    await vi.waitFor(() => expect(pane.el("speakd-reader-llm-test-status").textContent).toMatch(/401/));
  });

  it("reads the custom request flags from their checkboxes", () => {
    const pane = mountPane();
    const { api } = fakeApi();
    wirePrefsPane(pane.win, api);
    pane.el("speakd-reader-llm-custom-session").checked = true;
    pane.el("speakd-reader-llm-custom-thinking").checked = false;
    expect(readValues(pane.win)).toMatchObject({ customSessionHeader: true, customThinking: false });
  });
});
