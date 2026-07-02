/**
 * DexcostSpanProcessor — OpenTelemetry ingestion bridge (ONE-WAY, IN ONLY).
 *
 * Consumes LLM spans already emitted inside the application — the Vercel
 * AI SDK's `experimental_telemetry` spans and anything following the
 * GenAI semantic conventions — and converts them into dexcost cost
 * events, shipped through the SDK's normal buffer → pusher pipeline to
 * the dexcost endpoint ONLY.
 *
 * What this is NOT:
 * - It is not an exporter: nothing OTel-shaped leaves the process because
 *   of it, and no other tracing backend receives anything new.
 * - It is not a firehose: spans without LLM usage attributes are ignored,
 *   and prompt/completion CONTENT attributes are never read — only model,
 *   provider, token counts, and timing.
 *
 * Usage — existing OTel setup (the processor coexists with any exporters
 * already registered; each processor is independent):
 *
 *   import { NodeSDK } from "@opentelemetry/sdk-node";
 *   import { init, DexcostSpanProcessor } from "@dexcost/sdk";
 *
 *   init({ apiKey: process.env.DEXCOST_API_KEY });
 *   const sdk = new NodeSDK({ spanProcessors: [new DexcostSpanProcessor()] });
 *   sdk.start();
 *
 * Then enable the AI SDK's telemetry per call (v5/v6):
 *
 *   await generateText({ model, prompt, experimental_telemetry: { isEnabled: true } });
 *
 * Dependency-free: the SpanProcessor and ReadableSpan surfaces are
 * duck-typed, so no `@opentelemetry/*` package enters the SDK's
 * dependency tree (the app registering the processor has them already).
 *
 * Double-count safety: the SDK's other capture layers register a
 * fingerprint of every llm_call they record; span-derived events that
 * match a recent fingerprint for the same task are dropped, so running
 * the bridge alongside the patched fetch never double-bills a call.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask } from "../core/context.js";
import { getAmbientSessionTask } from "../core/session.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { wasLlmRecentlyCaptured, registerLlmCapture } from "../core/llm-dedup.js";
import { debugLog } from "../core/debug.js";
import type { CostTracker } from "../core/tracker.js";
import { getTracker } from "../core/tracker.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ---------------------------------------------------------------------------
// Attribute extraction
// ---------------------------------------------------------------------------

/**
 * Token attribute aliases across emitters and semconv revisions:
 * - AI SDK v4:      ai.usage.promptTokens / ai.usage.completionTokens
 * - AI SDK v5+:     ai.usage.inputTokens  / ai.usage.outputTokens
 * - GenAI semconv:  gen_ai.usage.input_tokens / gen_ai.usage.output_tokens
 * - older semconv:  gen_ai.usage.prompt_tokens / gen_ai.usage.completion_tokens
 */
const INPUT_TOKEN_KEYS = [
  "gen_ai.usage.input_tokens",
  "gen_ai.usage.prompt_tokens",
  "ai.usage.inputTokens",
  "ai.usage.promptTokens",
];
const OUTPUT_TOKEN_KEYS = [
  "gen_ai.usage.output_tokens",
  "gen_ai.usage.completion_tokens",
  "ai.usage.outputTokens",
  "ai.usage.completionTokens",
];
const CACHED_TOKEN_KEYS = [
  "gen_ai.usage.cached_input_tokens",
  "ai.usage.cachedInputTokens",
];
const MODEL_KEYS = [
  "gen_ai.response.model",
  "gen_ai.request.model",
  "ai.response.model",
  "ai.model.id",
];
const PROVIDER_KEYS = ["gen_ai.provider.name", "gen_ai.system", "ai.model.provider"];

function _firstNumber(attrs: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const v = attrs[key];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v !== "" && Number.isFinite(Number(v))) return Number(v);
  }
  return undefined;
}

function _firstString(attrs: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const v = attrs[key];
    if (typeof v === "string" && v) return v;
  }
  return undefined;
}

/** hrtime tuple [seconds, nanoseconds] → milliseconds; tolerant of absent times. */
function _spanLatencyMs(span: any): number | undefined {
  try {
    const start = span.startTime;
    const end = span.endTime;
    if (Array.isArray(start) && Array.isArray(end)) {
      const ms = (end[0] - start[0]) * 1_000 + (end[1] - start[1]) / 1_000_000;
      return ms >= 0 ? Math.round(ms) : undefined;
    }
  } catch {
    // absent/foreign time representation — latency omitted
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Processor
// ---------------------------------------------------------------------------

/** Options for {@link DexcostSpanProcessor}. */
export interface DexcostSpanProcessorOptions {
  /**
   * Tracker to record into. Defaults to the `init()` singleton, resolved
   * lazily per span so the processor can be constructed before init().
   */
  tracker?: CostTracker;
  /**
   * Disable the cross-layer double-count guard. Only do this when the
   * bridge is the ONLY capture layer in the process (trackHttp disabled,
   * no instruments, no middleware).
   */
  dedupe?: boolean;
}

/**
 * Structurally implements `@opentelemetry/sdk-trace-base`'s SpanProcessor.
 *
 * Failure posture: everything is exception-guarded — a malformed span or
 * a dexcost error can never break the host application's tracing.
 */
export class DexcostSpanProcessor {
  private readonly _options: DexcostSpanProcessorOptions;
  /** Task captured at span START (the ALS context the call runs in). */
  private readonly _taskBySpan = new WeakMap<object, Task>();
  private _warnedNoTracker = false;

  constructor(options: DexcostSpanProcessorOptions = {}) {
    this._options = options;
  }

  /** SpanProcessor.onStart — capture the active task while the ALS scope is live. */
  onStart(span: any, _parentContext?: unknown): void {
    try {
      const task = getCurrentTask();
      if (task && span && typeof span === "object") {
        this._taskBySpan.set(span, task);
      }
    } catch {
      // never break tracing
    }
  }

  /** SpanProcessor.onEnd — convert LLM spans into cost events. */
  onEnd(span: any): void {
    try {
      const attrs: Record<string, unknown> = span?.attributes ?? {};
      const inputTokens = _firstNumber(attrs, INPUT_TOKEN_KEYS);
      const outputTokens = _firstNumber(attrs, OUTPUT_TOKEN_KEYS);
      // Not an LLM usage span (HTTP span, DB span, an AI SDK parent span
      // whose child doGenerate carries the usage) — ignore.
      if (inputTokens === undefined && outputTokens === undefined) return;

      const tracker = this._resolveTracker();
      if (!tracker) return;

      const inTok = inputTokens ?? 0;
      const outTok = outputTokens ?? 0;

      // The task bound when the span started (same request context);
      // fall back to the current context, ambient session, then a
      // per-span auto-task so the cost is never dropped.
      let task = (span && this._taskBySpan.get(span)) ?? getCurrentTask();
      let autoCreated = false;
      if (!task) {
        task = getAmbientSessionTask("otel");
      }
      if (!task) {
        task = createAutoTask("otel.llm_span");
        tracker.buffer.upsertTask(task);
        autoCreated = true;
      }

      // Cross-layer double-count guard (fetch fallback / middleware /
      // instruments record BEFORE the span ends).
      if (this._options.dedupe !== false && wasLlmRecentlyCaptured(task.taskId, inTok, outTok)) {
        debugLog(
          "otel",
          `span skipped (already captured by another layer): in=${inTok} out=${outTok}`,
        );
        if (autoCreated) finalizeAutoTask(task, "success", tracker.buffer);
        return;
      }

      const model = _firstString(attrs, MODEL_KEYS) ?? "unknown";
      const provider = _firstString(attrs, PROVIDER_KEYS) ?? "otel";
      const cachedTokens = _firstNumber(attrs, CACHED_TOKEN_KEYS) ?? 0;
      const latencyMs = _spanLatencyMs(span);
      // OTel SpanStatusCode.ERROR === 2.
      const failed = span?.status?.code === 2;

      const hasUsage = inTok > 0 || outTok > 0;
      let costUsd: Decimal = new Decimal(0);
      let costConfidence: CostConfidence = "estimated";
      let pricingSource: PricingSource = "unknown";
      if (hasUsage) {
        const result = tracker.pricing.getCost(model, inTok, outTok, cachedTokens);
        costUsd = result.costUsd;
        costConfidence = result.costConfidence;
        pricingSource = result.pricingSource;
      }

      const event = createCostEvent({
        eventId: randomUUID(),
        taskId: task.taskId,
        eventType: "llm_call",
        costUsd,
        costConfidence,
        pricingSource,
        provider,
        model,
        inputTokens: inTok,
        outputTokens: outTok,
        cachedTokens,
        latencyMs,
        isRetry: false,
        details: { source: "otel_bridge", span_name: String(span?.name ?? "") },
      });
      tracker.buffer.addEvent(event);
      registerLlmCapture(task.taskId, inTok, outTok);

      task.llmCostUsd = task.llmCostUsd.plus(costUsd);
      task.totalCostUsd = task.totalCostUsd.plus(costUsd);
      task.totalInputTokens += inTok;
      task.totalOutputTokens += outTok;
      task.totalCachedTokens += cachedTokens;
      tracker.buffer.upsertTask(task);

      debugLog(
        "otel",
        `llm_call captured via otel bridge: ${provider}/${model} in=${inTok} out=${outTok}` +
          (failed ? " (span errored)" : ""),
      );
      if (autoCreated) {
        finalizeAutoTask(task, failed ? "failed" : "success", tracker.buffer);
      }
    } catch (err) {
      debugLog("otel", `span processing failed (span dropped, tracing unaffected): ${String(err)}`);
    }
  }

  /** SpanProcessor.forceFlush — push buffered events now. */
  async forceFlush(): Promise<void> {
    const tracker = this._resolveTracker();
    if (!tracker) return;
    try {
      await tracker.flush();
    } catch {
      // events stay buffered for the next cycle
    }
  }

  /** SpanProcessor.shutdown — final flush. */
  async shutdown(): Promise<void> {
    return this.forceFlush();
  }

  private _resolveTracker(): CostTracker | null {
    if (this._options.tracker) return this._options.tracker;
    try {
      return getTracker();
    } catch {
      if (!this._warnedNoTracker) {
        this._warnedNoTracker = true;
        // eslint-disable-next-line no-console
        console.warn(
          "[dexcost] DexcostSpanProcessor is registered but dexcost is not " +
            "initialized (init() was never called) — LLM spans are passing " +
            "through unrecorded.",
        );
      }
      return null;
    }
  }
}
