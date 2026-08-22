import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { ProviderOperationSession, wrapProviderStream } from "./provider-metering.js";
import { field, prefixedModel, tokenMeasurement } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const PROVIDERS: Record<string, string> = {
  openai: "openai", text_completion_openai: "openai",
  anthropic: "anthropic", claude: "anthropic",
  gemini: "google", google: "google", google_ai_studio: "google",
  vertex_ai: "google", vertex: "google",
  azure: "azure_openai", azure_text: "azure_openai", azure_openai: "azure_openai",
  azure_ai: "azure_ai", azure_ai_studio: "azure_ai",
  bedrock: "bedrock", aws_bedrock: "bedrock",
  cohere: "cohere", huggingface: "huggingface", hugging_face: "huggingface",
  together_ai: "together_ai", together: "together_ai",
  ollama: "ollama", ollama_chat: "ollama",
  mistral: "mistral", mistral_ai: "mistral",
  groq: "groq", openrouter: "openrouter", open_router: "openrouter", openrouter_ai: "openrouter",
  perplexity: "perplexity", perplexity_ai: "perplexity", fal: "fal", fal_ai: "fal",
};

export function classifyLiteLlmProvider(...candidates: unknown[]): string {
  for (const candidate of candidates) {
    if (typeof candidate !== "string" || candidate.length === 0) continue;
    const normalized = candidate.trim().toLowerCase().replaceAll("-", "_");
    if (PROVIDERS[normalized]) return PROVIDERS[normalized];
    const prefix = normalized.split("/", 1)[0] ?? normalized;
    if (PROVIDERS[prefix]) return PROVIDERS[prefix];
  }
  return "openai";
}

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
let providedModule: any;
let patched = false;

function patchMethod(
  owner: any,
  name: string,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  const original = owner[name] as (...args: any[]) => any;
  if (patches.some((patch) => patch.owner === owner && patch.name === name)) return;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const requestedModel = body?.model ?? args.find((item) => typeof item === "string");
    const provider = classifyLiteLlmProvider(
      body?.custom_llm_provider,
      body?.provider,
      requestedModel,
    );
    const model = prefixedModel(provider, requestedModel);
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `litellm.${name}`, provider, service: "litellm",
      operation: `litellm.${name}`, component: "llm", model, eventType: "llm_call",
    });
    let result: any;
    try {
      result = session.invoke(() => original.apply(this, args));
    } catch (error) {
      session.fail(error);
      throw error;
    }
    const complete = (response: any): any => {
      const resolvedProvider = classifyLiteLlmProvider(
        field(response, "_hidden_params", "custom_llm_provider"),
        response?.provider,
        provider,
      );
      const measurement = tokenMeasurement(response, model, resolvedProvider);
      measurement.responseModel = prefixedModel(resolvedProvider, measurement.responseModel ?? model);
      if (body?.stream) return wrapProviderStream(response, session, () => {}, () => measurement);
      session.finish(measurement);
      return response;
    };
    return result && typeof result.then === "function"
      ? result.then(complete, (error: unknown) => { session.fail(error); throw error; })
      : complete(result);
  };
  patches.push({ owner, name, original });
}

export async function instrumentLiteLlm(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional provider package
    mod = await import("litellm");
  }
  const roots = [mod, mod?.default, mod?.LiteLLM?.prototype, mod?.Client?.prototype];
  for (const owner of roots) {
    for (const name of ["completion", "acompletion", "chat", "generate", "embed", "embedding"]) {
      try { patchMethod(owner, name, pricing, buffer); } catch { /* immutable module namespace */ }
    }
  }
  if (patches.length === 0) throw new Error("litellm package exposes no supported completion surface");
  patched = true;
}

export function uninstrumentLiteLlm(): void {
  for (const patch of patches.splice(0)) patch.owner[patch.name] = patch.original;
  patched = false;
}

export function provideLiteLlmModule(ref: unknown): void { providedModule = ref; }

registerInstrument("litellm", instrumentLiteLlm, uninstrumentLiteLlm, provideLiteLlmModule);
