import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
} from "./provider-metering.js";
import {
  groqPricingLane,
  groqToolExecutionBlocksStaticPricing,
  tokenMeasurement,
} from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
let providedModule: any;
let patched = false;

function meteringResponse(response: any): any {
  return {
    id: response?.id,
    model: response?.model,
    service_tier: response?.service_tier,
    choices: response?.choices,
    usage: response?.usage ?? response?.x_groq?.usage,
    _dexcost_groq_tool_execution_seen: response?._dexcost_groq_tool_execution_seen,
  };
}

function measurement(
  response: any,
  requested: string,
  state?: { serviceTier?: unknown; toolExecutionSeen: boolean },
): OperationMeasurement {
  const normalized = meteringResponse(response);
  if (state !== undefined) {
    if ((normalized.service_tier === undefined || normalized.service_tier === null) &&
        state.serviceTier !== undefined) {
      normalized.service_tier = state.serviceTier;
    }
    normalized._dexcost_groq_tool_execution_seen = state.toolExecutionSeen;
  }
  const result = tokenMeasurement(normalized, requested, "groq");
  result.responseModel = typeof response?.model === "string" && response.model.length > 0
    ? response.model
    : requested;
  const lane = groqPricingLane(normalized);
  result.billingDimensions = [
    ["gateway", "groq"],
    ...(lane === undefined ? [] : [["groq_pricing_lane", lane] as const]),
  ];
  return result;
}

function patchCreate(owner: any, pricing: PricingEngine, buffer: EventBuffer): void {
  if (!owner || typeof owner.create !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === "create")) return;
  const original = owner.create as (...args: any[]) => any;
  owner.create = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const requested = typeof body?.model === "string" && body.model.length > 0 ? body.model : "unknown";
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: "groq.chat.completions.create",
      provider: "groq",
      service: "chat",
      operation: "groq.chat.completions.create",
      component: "llm",
      model: requested,
      eventType: "llm_call",
    });
    let result: any;
    try {
      result = session.invoke(() => original.apply(this, args));
    } catch (error) {
      session.fail(error);
      throw error;
    }
    const complete = (response: any): any => {
      if (body?.stream === true) {
        let terminal = response;
        const state = { serviceTier: body?.service_tier as unknown, toolExecutionSeen: false };
        return wrapProviderStream(response, session, (chunk) => {
          const item = chunk as any;
          if (item?.usage !== undefined || item?.x_groq?.usage !== undefined) terminal = item;
          if (item?.service_tier !== undefined && item?.service_tier !== null) {
            state.serviceTier = item.service_tier;
          }
          if (groqToolExecutionBlocksStaticPricing(item)) state.toolExecutionSeen = true;
        }, () => measurement(terminal, requested, state));
      }
      session.finish(measurement(response, requested, {
        serviceTier: body?.service_tier,
        toolExecutionSeen: false,
      }));
      return response;
    };
    return mapProviderResult(result, complete, (error) => {
      session.fail(error);
      throw error;
    });
  };
  patches.push({ owner, name: "create", original });
}

function discover(root: any, pricing: PricingEngine, buffer: EventBuffer): void {
  const seen = new Set<any>();
  const visit = (value: any, name: string, depth: number): void => {
    if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value) || depth > 5) return;
    seen.add(value);
    const lower = name.toLowerCase();
    const owner = typeof value === "function" ? value.prototype : value;
    if (lower.includes("chat") || lower.includes("completion")) {
      try { patchCreate(owner, pricing, buffer); } catch { /* immutable export */ }
    }
    for (const key of Object.keys(value).slice(0, 100)) {
      try { visit(value[key], `${name}.${key}`, depth + 1); } catch { /* getter side effect */ }
    }
  };
  visit(root, "groq", 0);
  if (root?.default !== root) visit(root?.default, "Groq", 0);
}

export async function instrumentGroq(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  const roots: any[] = [];
  if (providedModule !== undefined) roots.push(providedModule);
  else {
    // @ts-expect-error optional official SDK
    roots.push(await import("groq-sdk"));
    try {
      // @ts-expect-error optional official SDK subpath
      roots.push(await import("groq-sdk/resources/chat/completions"));
    } catch { /* older official packages still expose injectable client instances */ }
  }
  for (const root of roots) discover(root, pricing, buffer);
  if (patches.length === 0) {
    throw new Error(
      "official Groq SDK exposes no supported chat completion surface; " +
      "pass the client or module via instrumentModules.groq",
    );
  }
  patched = true;
}

export function uninstrumentGroq(): void {
  for (const item of patches.splice(0)) item.owner[item.name] = item.original;
  patched = false;
}

export function provideGroqModule(ref: unknown): void {
  providedModule = ref;
}

registerInstrument("groq", instrumentGroq, uninstrumentGroq, provideGroqModule);
