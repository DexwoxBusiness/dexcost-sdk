const LITELLM_PROVIDERS: Record<string, string> = {
  openai: "openai",
  text_completion_openai: "openai",
  anthropic: "anthropic",
  claude: "anthropic",
  gemini: "google",
  google: "google",
  google_ai_studio: "google",
  palm: "google",
  vertex_ai: "google",
  vertex: "google",
  azure: "azure_openai",
  azure_text: "azure_openai",
  azure_openai: "azure_openai",
  azure_ai: "azure_ai",
  azure_ai_studio: "azure_ai",
  bedrock: "bedrock",
  aws_bedrock: "bedrock",
  bedrock_converse: "bedrock",
  cohere: "cohere",
  huggingface: "huggingface",
  hugging_face: "huggingface",
  huggingface_hub: "huggingface",
  together_ai: "together",
  together: "together",
  ollama: "ollama",
  ollama_chat: "ollama",
  mistral: "mistral",
  mistral_ai: "mistral",
  groq: "groq",
  openrouter: "openrouter",
  open_router: "openrouter",
  openrouter_ai: "openrouter",
  perplexity: "perplexity",
  perplexity_ai: "perplexity",
  fal: "fal_ai",
  fal_ai: "fal_ai",
};

function normalizedProviderCandidate(value: unknown): string | undefined {
  if (typeof value !== "string" || value.trim().length === 0) return undefined;
  return value.trim().toLowerCase().replaceAll("-", "_");
}

/** Resolve the routed provider from LiteLLM provider/model identity. */
export function detectLiteLlmProvider(...candidates: unknown[]): string | undefined {
  for (const candidate of candidates) {
    const normalized = normalizedProviderCandidate(candidate);
    if (normalized === undefined) continue;
    const direct = LITELLM_PROVIDERS[normalized];
    if (direct !== undefined) return direct;
    const prefix = normalized.split("/", 1)[0] ?? normalized;
    const routed = LITELLM_PROVIDERS[prefix];
    if (routed !== undefined) return routed;
  }
  return undefined;
}

/** Backwards-compatible direct-package fallback. */
export function classifyLiteLlmProvider(...candidates: unknown[]): string {
  return detectLiteLlmProvider(...candidates) ?? "openai";
}

/** Match Python's stable provider/model identity for LiteLLM responses. */
export function canonicalLiteLlmModel(
  provider: string,
  responseModel: unknown,
  requestModel: unknown,
): string {
  const response = typeof responseModel === "string" ? responseModel.trim() : "";
  const request = typeof requestModel === "string" ? requestModel.trim() : "";
  const name = response || request || "unknown";
  if (provider === "together") {
    // Keep Together's provider-published API model ID. LiteLLM's provider
    // prefix describes the gateway route and must not become a pricing alias.
    for (const routedPrefix of ["together_ai/", "together/"]) {
      if (name.startsWith(routedPrefix)) return name.slice(routedPrefix.length);
    }
    return name;
  }
  const prefixes: Record<string, string> = {
    openrouter: "openrouter",
    azure_openai: "azure",
    azure_ai: "azure_ai",
    bedrock: "bedrock",
    fal_ai: "fal_ai",
    groq: "groq",
    huggingface: "huggingface",
    mistral: "mistral",
    ollama: "ollama",
    perplexity: "perplexity",
  };
  const prefix = provider === "google"
    ? (request.startsWith("vertex_ai/") ? "vertex_ai" : "gemini")
    : prefixes[provider];
  return prefix !== undefined && !name.startsWith(`${prefix}/`) ? `${prefix}/${name}` : name;
}

function environmentValue(name: string): string | undefined {
  try {
    const value = typeof process === "undefined" ? undefined : process.env?.[name];
    return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
  } catch {
    return undefined;
  }
}

/** Explicit DexCost name wins; the standard LiteLLM proxy variable remains compatible. */
export function configuredLiteLlmProxyUrl(): string | undefined {
  return environmentValue("DEXCOST_LITELLM_PROXY_URL") ?? environmentValue("LITELLM_PROXY_URL");
}

function normalizedProxyRoot(value: string): { origin: string; path: string } | undefined {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return undefined;
    let path = parsed.pathname.replace(/\/+$/u, "");
    path = path.replace(/\/(?:v1\/)?(?:chat\/completions|responses|embeddings)$/u, "");
    return { origin: parsed.origin.toLowerCase(), path: path.replace(/\/+$/u, "") };
  } catch {
    return undefined;
  }
}

/** Match either a LiteLLM base URL or a configured full inference endpoint. */
export function isConfiguredLiteLlmProxyUrl(
  candidate: string,
  configured = configuredLiteLlmProxyUrl(),
): boolean {
  if (configured === undefined) return false;
  const expected = normalizedProxyRoot(configured);
  const actual = normalizedProxyRoot(candidate);
  if (expected === undefined || actual === undefined || actual.origin !== expected.origin) return false;
  return expected.path.length === 0 ||
    actual.path === expected.path ||
    actual.path.startsWith(`${expected.path}/`);
}
