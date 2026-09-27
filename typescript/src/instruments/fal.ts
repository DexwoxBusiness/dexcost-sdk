import { AsyncLocalStorage } from "node:async_hooks";
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
import { nonNegativeDecimal, nonNegativeInteger } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
const inside = new AsyncLocalStorage<boolean>();
let providedModule: any;
let patched = false;

function application(args: any[]): string {
  const value = args[0]?.endpointId ?? args[0]?.application ?? args[0];
  return typeof value === "string" && value.length > 0 ? value : "unknown";
}
function model(applicationId: string, path?: unknown): string {
  let value = applicationId.replace(/^\/+|\/+$/g, "") || "unknown";
  if (typeof path === "string" && path.replaceAll("/", "")) value += `/${path.replace(/^\/+|\/+$/g, "")}`;
  if (value !== "unknown" && !value.startsWith("fal-ai/")) value = `fal-ai/${value}`;
  return `fal_ai/${value}`;
}
function mediaKind(applicationId: string, result?: any): "image" | "video" | "audio" | undefined {
  if (Array.isArray(result?.images) || result?.image) return "image";
  if (result?.video || result?.video_url) return "video";
  if (result?.audio || result?.audio_url) return "audio";
  const value = applicationId.toLowerCase();
  if (/(video|kling|veo|luma)/.test(value)) return "video";
  if (/(audio|music|speech|tts|whisper)/.test(value)) return "audio";
  if (/(image|flux|stable-diffusion|ideogram|recraft|imagen)/.test(value)) return "image";
  return undefined;
}
function inputArguments(args: any[]): Record<string, any> {
  const value = args[1]?.input ?? args[1]?.arguments ?? args[1] ?? args[0]?.input ?? {};
  return value && typeof value === "object" ? value : {};
}
function requestDimensions(app: string, input: Record<string, any>, path?: unknown): Array<readonly [string, string]> {
  const result: Array<readonly [string, string]> = [];
  const kind = mediaKind(app);
  if (kind) result.push(["media_type", kind]);
  if (typeof path === "string" && path.length > 0) result.push(["endpoint_path", path.slice(0, 256)]);
  for (const key of ["duration", "resolution", "aspect_ratio", "quality", "num_images"]) {
    const value = input[key];
    if (["string", "number", "bigint"].includes(typeof value)) result.push([key, String(value).slice(0, 256)]);
  }
  return result.slice(0, 24);
}
function measurement(result: any, modelId: string, app: string, input: Record<string, any>): OperationMeasurement {
  const lines: NonNullable<OperationMeasurement["usageLines"]> = [];
  const billingDimensions = requestDimensions(app, input);
  const images = Array.isArray(result?.images) ? result.images : result?.image ? [result.image] : [];
  if (images.length > 0) {
    lines.push({ metric: "output_image_count", quantity: images.length, unit: "Images" });
    const width = nonNegativeInteger(images[0]?.width);
    const height = nonNegativeInteger(images[0]?.height);
    if (width > 0) billingDimensions.push(["output_width", String(width)]);
    if (height > 0) billingDimensions.push(["output_height", String(height)]);
  }
  const kind = mediaKind(app, result);
  const videoSeconds = nonNegativeDecimal(result?.video?.duration ?? (kind === "video" ? result?.duration : undefined));
  const audioSeconds = nonNegativeDecimal(result?.audio?.duration ?? (kind === "audio" ? result?.duration : undefined));
  if (videoSeconds?.gt(0)) lines.push({ metric: "output_video_seconds", quantity: videoSeconds, unit: "Seconds" });
  if (audioSeconds?.gt(0)) lines.push({ metric: "output_audio_seconds", quantity: audioSeconds, unit: "Seconds" });
  const usage = result?.usage ?? {};
  const inputTokens = nonNegativeInteger(usage.prompt_tokens ?? usage.input_tokens);
  const outputTokens = nonNegativeInteger(usage.completion_tokens ?? usage.output_tokens);
  const cachedTokens = nonNegativeInteger(usage.cached_tokens);
  const ordinaryInputTokens = cachedTokens <= inputTokens ? inputTokens - cachedTokens : inputTokens;
  if (ordinaryInputTokens > 0) lines.push({ metric: "input_tokens", quantity: ordinaryInputTokens, unit: "Tokens" });
  if (cachedTokens > 0) lines.push({ metric: "cache_read_input_tokens", quantity: cachedTokens, unit: "Tokens" });
  if (outputTokens > 0) lines.push({ metric: "output_tokens", quantity: outputTokens, unit: "Tokens" });
  const cost = nonNegativeDecimal(usage.total_cost ?? usage.cost ?? result?.metrics?.cost ?? result?.cost);
  return {
    usageLines: lines, providerService: "inference",
    providerRecordId: result?.request_id ?? result?.requestId,
    // fal endpoints may bill per image, megapixel, video second, or GPU second,
    // and the billing-events API applies account-specific discounts. Keep the
    // native meters, but do not let the bundled legacy map become a second
    // monetary authority beside server reconciliation.
    pricingUsage: {},
    providerCostUsd: cost, responseModel: modelId,
    billingDimensions,
    inputTokens, outputTokens, cachedTokens,
  };
}
function status(result: any): ProviderJobStatus {
  const value = String(result?.status ?? "").toUpperCase();
  if (value === "IN_QUEUE") return "submitted";
  if (["IN_PROGRESS", "CANCELLATION_REQUESTED"].includes(value)) return "running";
  if (value === "COMPLETED") return "succeeded";
  if (["FAILED", "ERROR"].includes(value)) return "failed";
  if (["CANCELLED", "CANCELED"].includes(value)) return "cancelled";
  return "running";
}
function requestId(args: any[], result?: any): string | undefined {
  const value = result?.request_id ?? result?.requestId ?? args[1]?.requestId ?? args[1]?.request_id ?? args[1];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
function submitJob(buffer: EventBuffer, session: ProviderOperationSession, result: any, app: string, input: Record<string, any>, modelId: string): void {
  const id = requestId([], result);
  if (!id) return;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    taskId: session.task.taskId, provider: "fal_ai", service: "inference", providerRecordId: id,
    operation: "fal_ai.submit", component: "external", eventType: "external_cost",
    resourceType: "model", resourceId: modelId, status: "submitted",
    ownsTask: session.autoCreated, billingDimensions: requestDimensions(app, input),
  }));
  session.releaseForProviderJob();
}
function reconcile(pricing: PricingEngine, buffer: EventBuffer, result: any, id: string, terminalOverride?: ProviderJobStatus): void {
  const raw = buffer.getProviderJob("fal_ai", "inference", id);
  if (!raw) return;
  const previous = providerJobFromDict(raw);
  const nextStatus = terminalOverride ?? status(result);
  const app = previous.resourceId.replace(/^fal_ai\//, "");
  const meter = nextStatus === "succeeded" ? measurement(result?.data ?? result, previous.resourceId, app, {}) : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId, revision: previous.revision + 1,
    taskId: previous.taskId, provider: previous.provider, service: previous.service,
    providerRecordId: id, operation: previous.operation, component: previous.component,
    eventType: previous.eventType, resourceType: previous.resourceType, resourceId: previous.resourceId,
    status: nextStatus, submittedAt: previous.submittedAt, ownsTask: previous.ownsTask,
    billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(pricing, previous.resourceId, meter),
  }));
}

function patchMethod(owner: any, ownerName: string, name: string, pricing: PricingEngine, buffer: EventBuffer): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    if (inside.getStore()) return original.apply(this, args);
    const app = application(args);
    const input = inputArguments(args);
    const modelId = model(app, args[1]?.path);
    const isQueue = ownerName.toLowerCase().includes("queue") || ["submit", "status", "result", "cancel"].includes(name);
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `fal_ai.${name}`, provider: "fal_ai", service: "inference",
      operation: `fal_ai.${name}`, component: "external", model: modelId, eventType: "external_cost",
    });
    let output: any;
    try { output = inside.run(true, () => session.invoke(() => original.apply(this, args))); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (name === "submit") {
        submitJob(buffer, session, response, app, input, modelId);
      } else if (isQueue && ["status", "result", "cancel"].includes(name)) {
        const id = requestId(args, response);
        if (id) reconcile(pricing, buffer, response, id, name === "result" ? "succeeded" : undefined);
        session.finalizeWithoutEvent();
      } else if (name === "stream") {
        let final = response;
        return wrapProviderStream(response, session, (item) => { final = item; }, () => {
          const observed = measurement(final, modelId, app, input);
          observed.providerRecordId = requestId([], response) ?? observed.providerRecordId;
          return observed;
        });
      } else {
        const observed = measurement(response?.data ?? response, modelId, app, input);
        observed.providerRecordId = requestId([], response) ?? observed.providerRecordId;
        session.finish(observed);
      }
      return response;
    };
    return mapProviderResult(output, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}
function discover(root: any, pricing: PricingEngine, buffer: EventBuffer): void {
  const seen = new Set<any>();
  const visit = (value: any, name: string, depth: number): void => {
    if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value) || depth > 4) return;
    seen.add(value);
    const owner = typeof value === "function" ? value.prototype : value;
    for (const method of ["run", "subscribe", "stream", "submit", "status", "result", "cancel", "get", "fetch"]) {
      try { patchMethod(owner, name, method, pricing, buffer); } catch { /* immutable export */ }
    }
    for (const key of Object.keys(value).slice(0, 100)) {
      try { visit(value[key], `${name}.${key}`, depth + 1); } catch { /* getter */ }
    }
  };
  visit(root, "fal", 0);
  if (root?.fal) visit(root.fal, "fal", 0);
  if (root?.default !== root) visit(root?.default, "fal", 0);
}
export async function instrumentFal(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional official SDK
    mod = await import("@fal-ai/client");
  }
  discover(mod, pricing, buffer);
  if (patches.length === 0) throw new Error("@fal-ai/client exposes no supported surface; pass fal/client via instrumentModules.fal");
  patched = true;
}
export function uninstrumentFal(): void {
  for (const item of patches.splice(0)) item.owner[item.name] = item.original;
  patched = false;
}
export function provideFalModule(ref: unknown): void { providedModule = ref; }
registerInstrument("fal", instrumentFal, uninstrumentFal, provideFalModule);
