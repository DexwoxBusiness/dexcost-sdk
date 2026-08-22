import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  providerJobMeasurementFields,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
} from "./provider-metering.js";
import { nonNegativeInteger, prefixedModel, tokenMeasurement } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
let providedModule: any;
let patched = false;

type Kind = "responses" | "sonar" | "search" | "embeddings" | "contextualized_embeddings";

function kindFor(ownerName: string): Kind {
  const value = ownerName.toLowerCase();
  if (value.includes("contextualized")) return "contextualized_embeddings";
  if (value.includes("embedding")) return "embeddings";
  if (value.includes("search")) return "search";
  if (value.includes("chat") || value.includes("sonar")) return "sonar";
  return "responses";
}

function requestedModel(kind: Kind, body: any): string {
  if (kind === "search") return "perplexity/search";
  const selected = body?.model ?? (typeof body?.preset === "string" ? `preset/${body.preset}` : "unknown");
  return prefixedModel("perplexity", selected);
}

function measurement(kind: Kind, response: any, requested: string): OperationMeasurement {
  if (kind === "search") {
    return {
      usageLines: [
        { metric: "query_count", quantity: 1, unit: "Queries" },
        ...(Array.isArray(response?.results)
          ? [{ metric: "result_count", quantity: response.results.length, unit: "Results" }] : []),
      ],
      pricingUsage: { query_count: 1 },
      providerRecordId: response?.id ?? response?.response_id,
      responseModel: "perplexity/search", billingDimensions: [["gateway", "perplexity"]],
    };
  }
  const result = tokenMeasurement(response, requested, "perplexity");
  result.responseModel = prefixedModel("perplexity", result.responseModel ?? requested);
  const usage = response?.usage ?? {};
  const lines = [...(result.usageLines ?? [])];
  const pricingUsage: Record<string, string | number | bigint | import("../core/models.js").Decimal> = {
    ...(result.pricingUsage ?? {}),
  };
  const citations = nonNegativeInteger(usage.citation_tokens);
  const queries = nonNegativeInteger(usage.num_search_queries);
  if (citations > 0) {
    lines.push({ metric: "citation_tokens", quantity: citations, unit: "Tokens" });
    pricingUsage.citation_tokens = citations;
  }
  if (queries > 0) {
    lines.push({ metric: "query_count", quantity: queries, unit: "Queries" });
    pricingUsage.query_count = queries;
  }
  if (kind === "embeddings" || kind === "contextualized_embeddings") {
    const count = Array.isArray(response?.data)
      ? response.data.reduce((sum: number, item: any) => sum + (Array.isArray(item?.data) ? item.data.length : 1), 0)
      : 0;
    if (count > 0) lines.push({ metric: "embedding_count", quantity: count, unit: "Embeddings" });
  }
  if (usage.tool_calls_details && typeof usage.tool_calls_details === "object") {
    for (const [rawName, detail] of Object.entries(usage.tool_calls_details as Record<string, any>)) {
      const name = rawName.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
      const count = nonNegativeInteger(detail?.invocation);
      if (name && count > 0) {
        lines.push({ metric: `tool_${name}_invocation_count`, quantity: count, unit: "Calls" });
        if (["search_web", "web_search"].includes(name)) pricingUsage.web_search_calls = count;
      }
    }
  }
  result.usageLines = lines;
  result.pricingUsage = pricingUsage;
  const contextSize = usage.search_context_size;
  result.billingDimensions = [
    ["gateway", "perplexity"],
    ...(typeof contextSize === "string" ? [["search_context_size", contextSize] as const] : []),
  ];
  return result;
}

function jobStatus(response: any, cancelled = false): ProviderJobStatus {
  if (cancelled) return "cancelled";
  const value = String(response?.status ?? "").toLowerCase();
  if (["queued"].includes(value)) return "submitted";
  if (["in_progress", "requires_action", "cancelling"].includes(value)) return "running";
  if (["completed", "succeeded"].includes(value)) return "succeeded";
  if (["failed", "error"].includes(value)) return "failed";
  if (["cancelled", "canceled"].includes(value)) return "cancelled";
  return "running";
}

function insertNewJob(
  pricing: PricingEngine,
  buffer: EventBuffer,
  session: ProviderOperationSession,
  response: any,
  requested: string,
): void {
  const id = response?.id ?? response?.response_id;
  if (typeof id !== "string" || id.length === 0) return;
  const status = jobStatus(response);
  const meter = status === "succeeded" ? measurement("responses", response, requested) : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    taskId: session.task.taskId, provider: "perplexity", service: "responses",
    providerRecordId: id, operation: "perplexity.responses.create", component: "llm",
    eventType: "llm_call", resourceType: "model", resourceId: requested, status,
    ownsTask: session.autoCreated, billingDimensions: [["gateway", "perplexity"]],
    ...providerJobMeasurementFields(pricing, requested, meter),
  }));
  session.releaseForProviderJob();
}

function reconcileJob(pricing: PricingEngine, buffer: EventBuffer, response: any, id: string, cancelled: boolean): void {
  const raw = buffer.getProviderJob("perplexity", "responses", id);
  if (raw === undefined) return;
  const previous = providerJobFromDict(raw);
  const status = jobStatus(response, cancelled);
  const meter = status === "succeeded" ? measurement("responses", response, previous.resourceId) : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId, revision: previous.revision + 1,
    taskId: previous.taskId, provider: previous.provider, service: previous.service,
    providerRecordId: id, operation: previous.operation, component: previous.component,
    eventType: previous.eventType, resourceType: previous.resourceType,
    resourceId: previous.resourceId, status, submittedAt: previous.submittedAt,
    ownsTask: previous.ownsTask, billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(pricing, previous.resourceId, meter),
  }));
}

function patchMethod(owner: any, ownerName: string, name: string, pricing: PricingEngine, buffer: EventBuffer): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const kind = kindFor(ownerName);
    const body = args[0] ?? {};
    const id = typeof body === "string" ? body : body.response_id ?? body.responseId ?? args[0];
    const isReconcile = kind === "responses" && ["retrieve", "cancel"].includes(name);
    const requested = requestedModel(kind, body);
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `perplexity.${kind}.${name}`, provider: "perplexity", service: kind,
      operation: `perplexity.${kind}.${name}`, component: kind === "search" ? "external" : "llm",
      model: requested, eventType: kind === "search" ? "external_cost" : "llm_call",
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (isReconcile && typeof id === "string") {
        reconcileJob(pricing, buffer, response, id, name === "cancel");
        session.finalizeWithoutEvent();
        return response;
      }
      if (kind === "responses" && name === "create" && body.background === true) {
        insertNewJob(pricing, buffer, session, response, requested);
        return response;
      }
      if (body.stream === true) {
        let terminal = response;
        return wrapProviderStream(response, session, (chunk) => {
          const item = chunk as any;
          terminal = item?.response?.usage ? item.response : item;
        }, () => measurement(kind, terminal, requested));
      }
      session.finish(measurement(kind, response, requested), jobStatus(response) === "failed" ? "failed" : "succeeded");
      return response;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}

interface TransportRoute {
  kind: Kind;
  action: "create" | "retrieve" | "cancel";
  id?: string;
}

function transportRoute(verb: string, rawPath: unknown): TransportRoute | undefined {
  if (typeof rawPath !== "string") return undefined;
  const path = rawPath.split("?", 1)[0]?.replace(/\/$/, "") ?? "";
  if (verb === "post" && path === "/chat/completions") return { kind: "sonar", action: "create" };
  if (verb === "post" && path === "/search") return { kind: "search", action: "create" };
  if (verb === "post" && path === "/embeddings") return { kind: "embeddings", action: "create" };
  if (verb === "post" && path === "/contextualized-embeddings") {
    return { kind: "contextualized_embeddings", action: "create" };
  }
  if (verb === "post" && path === "/responses") return { kind: "responses", action: "create" };
  const cancel = /^\/responses\/([^/]+)\/cancel$/.exec(path);
  if (verb === "post" && cancel?.[1]) return {
    kind: "responses", action: "cancel", id: decodeURIComponent(cancel[1]),
  };
  const retrieve = /^\/responses\/([^/]+)$/.exec(path);
  if (verb === "get" && retrieve?.[1]) return {
    kind: "responses", action: "retrieve", id: decodeURIComponent(retrieve[1]),
  };
  return undefined;
}

function patchTransportMethod(
  owner: any,
  name: "post" | "get",
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const route = transportRoute(name, args[0]);
    const options = args[1];
    if (route === undefined || (options !== undefined && typeof options?.then === "function")) {
      return original.apply(this, args);
    }
    const body = options?.body ?? {};
    const requested = requestedModel(route.kind, body);
    const isReconcile = route.action !== "create";
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `perplexity.${route.kind}.${route.action}`,
      provider: "perplexity", service: route.kind,
      operation: `perplexity.${route.kind}.${route.action}`,
      component: route.kind === "search" ? "external" : "llm",
      model: requested, eventType: route.kind === "search" ? "external_cost" : "llm_call",
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (isReconcile && route.id !== undefined) {
        reconcileJob(pricing, buffer, response, route.id, route.action === "cancel");
        session.finalizeWithoutEvent();
        return response;
      }
      if (route.kind === "responses" && body.background === true) {
        insertNewJob(pricing, buffer, session, response, requested);
        return response;
      }
      if (body.stream === true) {
        let terminal = response;
        return wrapProviderStream(response, session, (chunk) => {
          const item = chunk as any;
          terminal = item?.response?.usage ? item.response : item;
        }, () => measurement(route.kind, terminal, requested));
      }
      session.finish(
        measurement(route.kind, response, requested),
        jobStatus(response) === "failed" ? "failed" : "succeeded",
      );
      return response;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}

function discover(root: any, pricing: PricingEngine, buffer: EventBuffer): void {
  const seen = new Set<any>();
  const visit = (value: any, name: string, depth: number): void => {
    if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value) || depth > 4) return;
    seen.add(value);
    const owner = typeof value === "function" ? value.prototype : value;
    const transportBacked = name.toLowerCase().includes("perplexity") &&
      typeof owner?.post === "function" && typeof owner?.get === "function";
    if (name.toLowerCase().includes("perplexity")) {
      try { patchTransportMethod(owner, "post", pricing, buffer); } catch { /* immutable export */ }
      try { patchTransportMethod(owner, "get", pricing, buffer); } catch { /* immutable export */ }
    }
    if (transportBacked) return;
    for (const method of ["create", "retrieve", "cancel"]) {
      try { patchMethod(owner, name, method, pricing, buffer); } catch { /* immutable export */ }
    }
    for (const key of Object.keys(value).slice(0, 100)) {
      try { visit(value[key], `${name}.${key}`, depth + 1); } catch { /* getter side effect */ }
    }
  };
  visit(root, "perplexity", 0);
  if (root?.default !== root) visit(root?.default, "Perplexity", 0);
}

export async function instrumentPerplexity(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional official SDK
    mod = await import("@perplexity-ai/perplexity_ai");
  }
  discover(mod, pricing, buffer);
  if (patches.length === 0) {
    throw new Error("official Perplexity SDK exposes no supported surface; pass a client via instrumentModules.perplexity");
  }
  patched = true;
}
export function uninstrumentPerplexity(): void {
  for (const item of patches.splice(0)) item.owner[item.name] = item.original;
  patched = false;
}
export function providePerplexityModule(ref: unknown): void { providedModule = ref; }
registerInstrument("perplexity", instrumentPerplexity, uninstrumentPerplexity, providePerplexityModule);
