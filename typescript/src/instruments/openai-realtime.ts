import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import {
  ProviderOperationSession,
  type OperationMeasurement,
  type ProviderOperationStatus,
  type ProviderUsageLine,
} from "./provider-metering.js";
import { recordOpenAIResponseTools } from "./openai-modern.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface Patch {
  owner: any;
  name: string;
  original: (...args: any[]) => any;
}

interface RealtimeState {
  model: string;
  pending: ProviderOperationSession[];
  active: Map<string, ProviderOperationSession>;
  seen: Set<string>;
  socketObserved: boolean;
  transportError?: unknown;
}

const patches: Patch[] = [];
const states = new WeakMap<object, RealtimeState>();

function realtimeModel(value: unknown): string {
  return typeof value === "string" && value.length > 0 ? value : "unknown";
}

function modelFromConnection(connection: any): string {
  try {
    const url = connection?.url instanceof URL ? connection.url : new URL(String(connection?.url));
    return realtimeModel(url.searchParams.get("model"));
  } catch {
    return "unknown";
  }
}

function stateFor(connection: any): RealtimeState {
  let state = typeof connection === "object" && connection !== null ? states.get(connection) : undefined;
  if (state === undefined) {
    state = {
      model: modelFromConnection(connection),
      pending: [],
      active: new Map(),
      seen: new Set(),
      socketObserved: false,
    };
    if (typeof connection === "object" && connection !== null) states.set(connection, state);
  }
  observeSocket(connection, state);
  return state;
}

function observeSocket(connection: any, state: RealtimeState): void {
  if (state.socketObserved) return;
  const socket = connection?.socket;
  if (socket === undefined || socket === null) return;
  state.socketObserved = true;
  const onError = (error: unknown): void => { state.transportError = error; };
  const onClose = (): void => {
    if (state.transportError !== undefined) failSessions(state, state.transportError);
    else cancelSessions(state);
  };
  if (typeof socket.addEventListener === "function") {
    socket.addEventListener("error", onError);
    socket.addEventListener("close", onClose);
  } else if (typeof socket.on === "function") {
    socket.on("error", onError);
    socket.on("close", onClose);
  }
}

function newSession(
  state: RealtimeState,
  pricing: PricingEngine,
  buffer: EventBuffer,
): ProviderOperationSession {
  return new ProviderOperationSession(pricing, buffer, {
    taskType: "openai.realtime.response",
    provider: "openai",
    service: "realtime",
    operation: "openai.realtime.response",
    component: "llm",
    model: state.model,
    eventType: "llm_call",
  });
}

function optionalCount(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function addUsage(
  pricingUsage: Record<string, number>,
  usageLines: ProviderUsageLine[],
  metric: string,
  quantity: number | undefined,
): void {
  if (quantity === undefined) return;
  pricingUsage[metric] = quantity;
  usageLines.push({ metric, quantity, unit: "Tokens" });
}

export function openAIRealtimeMeasurement(response: any, model: string): OperationMeasurement {
  const usage = response?.usage;
  const inputTokens = optionalCount(usage?.input_tokens);
  const outputTokens = optionalCount(usage?.output_tokens);
  const inputDetails = usage?.input_token_details;
  const outputDetails = usage?.output_token_details;
  const cachedTokens = optionalCount(inputDetails?.cached_tokens);
  const cachedDetails = inputDetails?.cached_tokens_details;
  const pricingUsage: Record<string, number> = {};
  const usageLines: ProviderUsageLine[] = [];

  const inputParts = {
    text: optionalCount(inputDetails?.text_tokens),
    audio: optionalCount(inputDetails?.audio_tokens),
    image: optionalCount(inputDetails?.image_tokens),
  };
  const cachedParts = {
    text: optionalCount(cachedDetails?.text_tokens),
    audio: optionalCount(cachedDetails?.audio_tokens),
    image: optionalCount(cachedDetails?.image_tokens),
  };
  const inputMetrics = {
    text: ["input_tokens", "cache_read_input_tokens"],
    audio: ["input_audio_tokens", "cache_read_input_audio_tokens"],
    image: ["input_image_tokens", "cache_read_input_image_tokens"],
  } as const;
  const hasCachedUsage = cachedTokens !== undefined && cachedTokens > 0;
  const classifiedModalities = (Object.keys(inputParts) as Array<keyof typeof inputParts>)
    .filter((name) => inputParts[name] !== undefined);
  const hasCompleteCachedSplit = !hasCachedUsage || classifiedModalities.every(
    (name) => cachedParts[name] !== undefined,
  );
  let classifiedInput = 0;
  for (const name of Object.keys(inputParts) as Array<keyof typeof inputParts>) {
    const total = inputParts[name];
    if (total === undefined) continue;
    classifiedInput += total;
    if (hasCompleteCachedSplit) {
      const cached = cachedParts[name] ?? 0;
      if (cached <= total) {
        addUsage(pricingUsage, usageLines, inputMetrics[name][0], total - cached);
        addUsage(pricingUsage, usageLines, inputMetrics[name][1], cached);
        continue;
      }
    }
    addUsage(pricingUsage, usageLines, `realtime_input_${name}_tokens_gross`, total);
  }
  if (inputTokens !== undefined) {
    const remainder = Math.max(0, inputTokens - classifiedInput);
    if (remainder > 0 || classifiedModalities.length === 0) {
      addUsage(
        pricingUsage,
        usageLines,
        "realtime_unclassified_input_tokens",
        classifiedInput > 0 ? remainder : inputTokens,
      );
    }
  }
  if (hasCachedUsage && !hasCompleteCachedSplit) {
    addUsage(pricingUsage, usageLines, "realtime_unclassified_cached_input_tokens", cachedTokens);
  }

  const outputParts = {
    text: optionalCount(outputDetails?.text_tokens),
    audio: optionalCount(outputDetails?.audio_tokens),
  };
  const outputMetrics = { text: "output_tokens", audio: "output_audio_tokens" } as const;
  let classifiedOutput = 0;
  let hasOutputPart = false;
  for (const name of Object.keys(outputParts) as Array<keyof typeof outputParts>) {
    const total = outputParts[name];
    if (total === undefined) continue;
    hasOutputPart = true;
    classifiedOutput += total;
    addUsage(pricingUsage, usageLines, outputMetrics[name], total);
  }
  if (outputTokens !== undefined) {
    const remainder = Math.max(0, outputTokens - classifiedOutput);
    if (remainder > 0 || !hasOutputPart) {
      addUsage(
        pricingUsage,
        usageLines,
        "realtime_unclassified_output_tokens",
        classifiedOutput > 0 ? remainder : outputTokens,
      );
    }
  }
  if (inputTokens !== undefined && classifiedInput > inputTokens) {
    addUsage(pricingUsage, usageLines, "realtime_input_usage_inconsistent", 1);
  }
  if (outputTokens !== undefined && classifiedOutput > outputTokens) {
    addUsage(pricingUsage, usageLines, "realtime_output_usage_inconsistent", 1);
  }

  return {
    pricingUsage,
    usageLines,
    providerRecordId: typeof response?.id === "string" ? response.id : undefined,
    responseModel: realtimeModel(model),
    modelCandidates: [`openai/${realtimeModel(model)}`, realtimeModel(model)],
    inputTokens,
    outputTokens,
    cachedTokens,
  };
}

function terminalStatus(response: any): ProviderOperationStatus {
  if (response?.status === "completed") return "succeeded";
  if (response?.status === "cancelled") return "cancelled";
  if (response?.status === "failed") return "failed";
  return "unknown";
}

function responseId(response: any): string | undefined {
  return typeof response?.id === "string" && response.id.length > 0 ? response.id : undefined;
}

function observeEvent(
  connection: any,
  event: any,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  const state = stateFor(connection);
  if ((event?.type === "session.created" || event?.type === "session.updated") && state.model === "unknown") {
    state.model = realtimeModel(event?.session?.model);
  }
  if (event?.type !== "response.created" && event?.type !== "response.done") return;
  const id = responseId(event?.response);
  if (id === undefined) return;
  if (event.type === "response.created") {
    if (state.active.has(id) || state.seen.has(id)) return;
    state.active.set(id, state.pending.shift() ?? newSession(state, pricing, buffer));
    return;
  }
  if (state.seen.has(id)) return;
  const session = state.active.get(id) ?? state.pending.shift() ?? newSession(state, pricing, buffer);
  state.active.delete(id);
  state.seen.add(id);
  session.finish(openAIRealtimeMeasurement(event.response, state.model), terminalStatus(event.response));
  recordOpenAIResponseTools(event.response, session.task, pricing, buffer);
}

function drainSessions(state: RealtimeState): ProviderOperationSession[] {
  const sessions = [...state.pending, ...state.active.values()];
  state.pending.length = 0;
  state.active.clear();
  return sessions;
}

function failSessions(state: RealtimeState, error: unknown): void {
  for (const session of drainSessions(state)) session.fail(error);
}

function cancelSessions(state: RealtimeState): void {
  for (const session of drainSessions(state)) session.cancel({ usageLines: [], pricingUsage: {} });
}

function patchMethod(owner: any, name: string, replacement: (original: (...args: any[]) => any) => (...args: any[]) => any): void {
  if (owner === undefined || owner === null || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = replacement(original);
  patches.push({ owner, name, original });
}

function patchRealtimeClass(value: any, pricing: PricingEngine, buffer: EventBuffer): void {
  const prototype = value?.prototype;
  if (prototype === undefined) return;
  patchMethod(prototype, "send", (original) => function (this: any, event: any): any {
    const state = stateFor(this);
    const session = event?.type === "response.create" ? newSession(state, pricing, buffer) : undefined;
    try {
      const result = original.call(this, event);
      if (session !== undefined) state.pending.push(session);
      return result;
    } catch (error) {
      session?.fail(error);
      throw error;
    }
  });
  patchMethod(prototype, "_emit", (original) => function (this: any, name: any, event: any, ...rest: any[]): any {
    if (name === "event") {
      try { observeEvent(this, event, pricing, buffer); } catch { /* fail-open provider hot path */ }
    }
    return original.call(this, name, event, ...rest);
  });
  patchMethod(prototype, "close", (original) => function (this: any, ...args: any[]): any {
    cancelSessions(stateFor(this));
    return original.apply(this, args);
  });
}

/** Patch either official Realtime transport module without taking a hard dependency on OpenAI. */
export function installOpenAIRealtime(module: any, pricing: PricingEngine, buffer: EventBuffer): boolean {
  const before = patches.length;
  const root = module?.default ?? module;
  patchRealtimeClass(root?.OpenAIRealtimeWS ?? module?.OpenAIRealtimeWS, pricing, buffer);
  patchRealtimeClass(root?.OpenAIRealtimeWebSocket ?? module?.OpenAIRealtimeWebSocket, pricing, buffer);
  return patches.length > before;
}

export function uninstallOpenAIRealtime(): void {
  for (const item of patches.splice(0).reverse()) item.owner[item.name] = item.original;
}
