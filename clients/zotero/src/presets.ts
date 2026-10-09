// Provider presets: the handful of known OpenAI-compatible endpoints, with the
// request flavour each one needs. Pure, so the pane and the settings resolver
// can share it and the tests can read it.
//
// The flavour exists because opencode-go wants an `x-opencode-session` routing
// header and a DeepSeek-style `thinking` field. OpenAI rejects unknown body
// fields with a 400, so those two may only be sent to endpoints that expect
// them.

/** The per-provider request shape: the two opencode-specific additions. */
export interface RequestProfile {
  /** Send the `x-opencode-session` routing header. */
  sessionHeader: boolean;
  /** Send the `thinking: { type }` body field. */
  thinking: boolean;
}

export interface ProviderPreset {
  id: string;
  label: string;
  /** Empty for the custom entry, which leaves the URL to the user. */
  baseUrl: string;
  defaultModel: string;
  /** Models offered in the pane's picker. Empty means "type your own". */
  models: string[];
  profile: RequestProfile;
}

export const OPENCODE_PROFILE: RequestProfile = { sessionHeader: true, thinking: true };
export const GENERIC_PROFILE: RequestProfile = { sessionHeader: false, thinking: false };

/** The id of the "type your own endpoint" entry. */
export const CUSTOM_ID = "custom";

// Ordered as they appear in the pane. Model ids checked against each
// provider's own docs/API on 2026-10-08; they go stale, so the Custom entry in
// the model picker is the escape hatch.
//
// Sources:
//   opencode-go — opencode.ai/zen model catalogue
//   OpenAI      — platform.openai.com/docs/models (gpt-5.6-sol is the default;
//                 terra/luna also appear in the opencode-go catalogue)
//   OpenRouter  — openrouter.ai/api/v1/models
//   DeepSeek    — api-docs.deepseek.com (deepseek-flash, deepseek-v4-pro)
//   Gemini      — ai.google.dev/gemini-api/docs/models
//   Groq        — console.groq.com/docs/models
//   Mistral     — docs.mistral.ai model ids (mistral-large-4-0, …)
//   xAI         — docs.x.ai/docs/models (grok-4.7 is the flagship)
//   Ollama      — ollama.com/library
//   LM Studio   — no fixed catalogue; whatever the user has loaded
export const PRESETS: readonly ProviderPreset[] = [
  {
    id: "opencode",
    label: "opencode-go",
    baseUrl: "https://opencode.ai/zen/go/v1",
    defaultModel: "deepseek-v4.1-flash",
    models: [
      "deepseek-v4.1-flash",
      "deepseek-v4-pro",
      "claude-sonnet-5-5",
      "gpt-5.6-sol",
      "gemini-3.8-flash",
      "grok-4.7",
      "glm-5.3",
      "qwen3.8-max",
    ],
    profile: OPENCODE_PROFILE,
  },
  {
    id: "openai",
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    defaultModel: "gpt-5.6-sol",
    models: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    defaultModel: "deepseek/deepseek-v4.1-flash",
    models: [
      "deepseek/deepseek-v4.1-flash",
      "deepseek/deepseek-v4-pro",
      "google/gemini-3.8-flash",
      "google/gemma-4-31b-it:free",
    ],
    profile: GENERIC_PROFILE,
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com",
    defaultModel: "deepseek-flash",
    models: ["deepseek-flash", "deepseek-v4-pro"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "gemini",
    label: "Google Gemini",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    defaultModel: "gemini-3.8-flash",
    models: ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "groq",
    label: "Groq",
    baseUrl: "https://api.groq.com/openai/v1",
    defaultModel: "llama-3.3-70b-versatile",
    models: ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "mistral",
    label: "Mistral",
    baseUrl: "https://api.mistral.ai/v1",
    defaultModel: "mistral-large-4-0",
    models: ["mistral-large-4-0", "mistral-medium-3-5-26-04", "mistral-small-4-0-26-03"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "xai",
    label: "xAI (Grok)",
    baseUrl: "https://api.x.ai/v1",
    defaultModel: "grok-4.7",
    models: ["grok-4.7", "grok-4.6", "grok-4.5"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "ollama",
    label: "Ollama (local)",
    baseUrl: "http://localhost:11434/v1",
    defaultModel: "llama3.2",
    models: ["llama3.2", "gemma4", "qwen3.8", "llama3.3", "deepseek-v3"],
    profile: GENERIC_PROFILE,
  },
  {
    id: "lmstudio",
    label: "LM Studio (local)",
    baseUrl: "http://localhost:1234/v1",
    defaultModel: "",
    // Whatever the user has loaded; there is no fixed catalogue.
    models: [],
    profile: GENERIC_PROFILE,
  },
  {
    id: CUSTOM_ID,
    label: "Custom",
    baseUrl: "",
    defaultModel: "",
    models: [],
    profile: GENERIC_PROFILE,
  },
];

export function presetById(id: string): ProviderPreset | undefined {
  return PRESETS.find((preset) => preset.id === id);
}

function normaliseUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

export function presetForBaseUrl(baseUrl: string): ProviderPreset | undefined {
  const wanted = normaliseUrl(baseUrl);
  if (wanted === "") return undefined;
  return PRESETS.find((preset) => preset.baseUrl !== "" && normaliseUrl(preset.baseUrl) === wanted);
}

/**
 * The flavour to send for a resolved endpoint. The explicitly chosen provider
 * wins; with none (an install from before providers existed) the base URL is
 * matched; failing both, the user's custom flags decide.
 */
export function resolveProfile(providerId: string, baseUrl: string, custom: RequestProfile): RequestProfile {
  if (providerId === CUSTOM_ID) return custom;
  const preset = presetById(providerId) ?? presetForBaseUrl(baseUrl);
  return preset ? preset.profile : custom;
}
