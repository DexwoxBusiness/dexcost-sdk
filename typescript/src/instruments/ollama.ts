import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { ProviderOperationSession, wrapProviderStream, type OperationMeasurement } from "./provider-metering.js";
import { nonNegativeInteger, prefixedModel } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
let providedModule: any;
let patched = false;

function measurement(response: any, model: string): OperationMeasurement {
  const input = nonNegativeInteger(response?.prompt_eval_count);
  const output = nonNegativeInteger(response?.eval_count);
  const duration = Number(response?.total_duration ?? 0) / 1_000_000_000;
  const lines = [
    ...(input > 0 ? [{ metric: "input_tokens", quantity: input, unit: "Tokens" }] : []),
    ...(output > 0 ? [{ metric: "output_tokens", quantity: output, unit: "Tokens" }] : []),
    ...(duration > 0 ? [{ metric: "compute_seconds", quantity: String(duration), unit: "Seconds" }] : []),
  ];
  return {
    usageLines: lines, responseModel: prefixedModel("ollama", response?.model ?? model),
    inputTokens: input, outputTokens: output,
    billingDimensions: [["runtime", "local"]],
  };
}

function patch(owner: any, name: string, pricing: PricingEngine, buffer: EventBuffer): void {
  if (!owner || typeof owner[name] !== "function") return;
  const original = owner[name] as (...args: any[]) => any;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const model = prefixedModel("ollama", body.model ?? "unknown");
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `ollama.${name}`, provider: "ollama", service: "ollama",
      operation: `ollama.${name}`, component: "llm", model, eventType: "llm_call",
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (body.stream) {
        let final = response;
        return wrapProviderStream(
          response, session, (chunk) => { if ((chunk as any)?.done) final = chunk; },
          () => measurement(final, model),
        );
      }
      session.finish(measurement(response, model));
      return response;
    };
    return result && typeof result.then === "function"
      ? result.then(complete, (error: unknown) => { session.fail(error); throw error; })
      : complete(result);
  };
  patches.push({ owner, name, original });
}

export async function instrumentOllama(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional provider package
    mod = await import("ollama");
  }
  for (const owner of [mod, mod?.default, mod?.Ollama?.prototype]) {
    for (const name of ["chat", "generate", "embed", "embeddings"]) {
      try { patch(owner, name, pricing, buffer); } catch { /* immutable ESM namespace */ }
    }
  }
  if (patches.length === 0) throw new Error("ollama package exposes no supported generation surface");
  patched = true;
}

export function uninstrumentOllama(): void {
  for (const item of patches.splice(0)) item.owner[item.name] = item.original;
  patched = false;
}
export function provideOllamaModule(ref: unknown): void { providedModule = ref; }
registerInstrument("ollama", instrumentOllama, uninstrumentOllama, provideOllamaModule);
