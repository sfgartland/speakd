// The preference pane's wiring: the provider picker, the model picker and the
// Test button. Kept apart from src/index.ts so the behaviour can be driven in a
// DOM test; the pane supplies its `window` and a small api.

import { CUSTOM_ID, PRESETS, presetById, presetForBaseUrl, type ProviderPreset } from "./presets";
import { LLM_ENDPOINT_PREF, LLM_MODEL_PREF, LLM_PROVIDER_PREF } from "./prefs";

export interface PrefsValues {
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  customSessionHeader: boolean;
  customThinking: boolean;
}

export interface PrefsTestResult {
  ok: boolean;
  ms: number;
  message: string;
}

/** The bit of Zotero the pane needs: read and write prefs, and run a test. */
export interface PrefsPaneApi {
  getPref(key: string): string | boolean;
  setPref(key: string, value: string | boolean): void;
  test(values: PrefsValues): Promise<PrefsTestResult>;
}

export interface ModelOption {
  value: string;
  label: string;
}

/** The model picker's options: the preset's models, then the custom entry. */
export function modelOptions(preset: ProviderPreset | undefined): ModelOption[] {
  const models = preset?.models ?? [];
  return [...models.map((value) => ({ value, label: value })), { value: CUSTOM_ID, label: "Custom…" }];
}

/** The provider id to show for a base URL: the chosen preset, else the URL's
 * match, else custom. */
export function providerIdFor(baseUrl: string, provider: string): string {
  if (provider !== "" && presetById(provider)) return provider;
  return presetForBaseUrl(baseUrl)?.id ?? CUSTOM_ID;
}

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const XUL_NS = "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul";

/** The pane's current (possibly unsaved) values. */
export function readValues(win: Any): PrefsValues {
  const byId = (id: string): Any => win.document.getElementById(id);
  const typed = String(byId("speakd-reader-llm-model-custom")?.value ?? "").trim();
  const picked = String(byId("speakd-reader-llm-model")?.value ?? "");
  return {
    provider: String(byId("speakd-reader-llm-provider")?.value ?? ""),
    baseUrl: String(byId("speakd-reader-llm-base-url")?.value ?? "").trim(),
    // The picker's "custom" entry is a menu label, not a model name.
    model: typed !== "" ? typed : picked === CUSTOM_ID ? "" : picked,
    apiKey: String(byId("speakd-reader-llm-key")?.value ?? ""),
    customSessionHeader: Boolean(byId("speakd-reader-llm-custom-session")?.checked),
    customThinking: Boolean(byId("speakd-reader-llm-custom-thinking")?.checked),
  };
}

function menuItem(doc: Any): Any {
  // A preference pane is parsed with XUL as the default namespace, and a XUL
  // `menupopup` only renders XUL-namespaced `menuitem` children.
  // `document.createElement('menuitem')` makes a null/HTML-namespaced element,
  // so the dropdown comes up empty. `createXULElement` is Zotero's idiom;
  // `createElementNS` is the equivalent that also works in the DOM test
  // environment.
  return typeof doc.createXULElement === "function"
    ? doc.createXULElement("menuitem")
    : doc.createElementNS(XUL_NS, "menuitem");
}

function fillMenu(doc: Any, menu: Any, items: ModelOption[]): void {
  if (!menu) return;
  menu.textContent = "";
  for (const item of items) {
    const node = menuItem(doc);
    node.setAttribute("label", item.label);
    node.setAttribute("value", item.value);
    node.value = item.value;
    menu.appendChild(node);
  }
}

/**
 * Wire the pane's controls. Idempotent enough to call once per pane open: it
 * fills the pickers from the stored prefs and adds the listeners.
 */
export function wirePrefsPane(win: Any, api: PrefsPaneApi): void {
  const doc = win.document;
  const byId = (id: string): Any => doc.getElementById(id);
  const providerEl = byId("speakd-reader-llm-provider");
  const providerMenu = byId("speakd-reader-llm-provider-menu");
  const baseUrlEl = byId("speakd-reader-llm-base-url");
  const modelEl = byId("speakd-reader-llm-model");
  const modelMenu = byId("speakd-reader-llm-model-menu");
  const modelCustomEl = byId("speakd-reader-llm-model-custom");
  const testButton = byId("speakd-reader-llm-test");
  const statusEl = byId("speakd-reader-llm-test-status");
  const customBox = byId("speakd-reader-llm-custom-profile");
  if (!providerEl || !modelEl || !testButton) return;

  const presetModels = (id: string): string[] => presetById(id)?.models ?? [];

  const populateModels = (providerId: string, currentModel: string): string => {
    const preset = presetById(providerId);
    fillMenu(doc, modelMenu, modelOptions(preset));
    const known = preset?.models ?? [];
    const model = known.includes(currentModel) ? currentModel : preset?.defaultModel || currentModel;
    modelEl.value = known.includes(model) ? model : CUSTOM_ID;
    if (modelCustomEl) {
      modelCustomEl.value = model;
      modelCustomEl.hidden = known.includes(model);
    }
    return model;
  };

  const showProvider = (providerId: string, currentModel: string, persist: boolean): void => {
    const preset = presetById(providerId);
    if (preset && preset.baseUrl !== "") {
      if (baseUrlEl) baseUrlEl.value = preset.baseUrl;
      if (persist) api.setPref(LLM_ENDPOINT_PREF, preset.baseUrl);
    }
    if (persist) api.setPref(LLM_PROVIDER_PREF, providerId);
    const model = populateModels(providerId, currentModel);
    if (persist) api.setPref(LLM_MODEL_PREF, model);
    if (customBox) customBox.hidden = providerId !== CUSTOM_ID;
  };

  // --- Initial state ------------------------------------------------------
  fillMenu(
    doc,
    providerMenu,
    PRESETS.map((preset) => ({ value: preset.id, label: preset.label })),
  );
  const storedProvider = String(api.getPref(LLM_PROVIDER_PREF) ?? "");
  const storedBaseUrl = String(baseUrlEl?.value || api.getPref(LLM_ENDPOINT_PREF) || "");
  const providerId = providerIdFor(storedBaseUrl, storedProvider);
  providerEl.value = providerId;
  const storedModel = String(api.getPref(LLM_MODEL_PREF) ?? "") || String(modelCustomEl?.value ?? "");
  showProvider(providerId, storedModel, false);

  // --- Listeners ----------------------------------------------------------
  providerEl.addEventListener("command", () => {
    const next = String(providerEl.value || "");
    const current = String(modelCustomEl?.value ?? "");
    showProvider(next, current, true);
    if (next === CUSTOM_ID) modelCustomEl?.focus?.();
  });

  modelEl.addEventListener("command", () => {
    const picked = String(modelEl.value || "");
    if (picked === CUSTOM_ID) {
      if (modelCustomEl) {
        modelCustomEl.value = String(api.getPref(LLM_MODEL_PREF) ?? modelCustomEl.value);
        modelCustomEl.hidden = false;
        modelCustomEl.focus?.();
      }
      return;
    }
    if (modelCustomEl) {
      modelCustomEl.value = picked;
      modelCustomEl.hidden = true;
    }
    api.setPref(LLM_MODEL_PREF, picked);
  });

  modelCustomEl?.addEventListener("change", () => {
    api.setPref(LLM_MODEL_PREF, String(modelCustomEl.value ?? ""));
    modelEl.value = presetModels(String(providerEl.value)).includes(String(modelCustomEl.value))
      ? String(modelCustomEl.value)
      : CUSTOM_ID;
  });

  testButton.addEventListener("click", async () => {
    if (testButton.disabled) return;
    testButton.disabled = true;
    if (statusEl) statusEl.textContent = "Testing…";
    try {
      const result = await api.test(readValues(win));
      if (statusEl) statusEl.textContent = `${result.ok ? "✓" : "✗"} ${result.message}`;
    } catch (error) {
      if (statusEl) {
        statusEl.textContent = `✗ ${error instanceof Error ? error.message : String(error)}`;
      }
    } finally {
      testButton.disabled = false;
    }
  });
}
