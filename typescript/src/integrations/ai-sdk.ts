/**
 * Vercel AI SDK model middleware — the SUPPORTED capture path for `ai` >= 5.
 *
 * The `ai` package ships ESM-only builds since v5 (getter-only exports in
 * its CJS shim), so module-level monkey-patching is structurally
 * impossible there. The AI SDK's sanctioned interception point is
 * `wrapLanguageModel` middleware, which sees exact request params and
 * native usage on both generate and stream, and survives every bundler
 * and module system.
 *
 * Usage:
 *
 *   import { wrapLanguageModel } from "ai";
 *   import { anthropic } from "@ai-sdk/anthropic";
 *   import { dexcostAiMiddleware } from "@dexcost/sdk";
 *
 *   const model = wrapLanguageModel({
 *     model: anthropic("claude-sonnet-4-5"),
 *     middleware: dexcostAiMiddleware(),
 *   });
 *   // use `model` with generateText / streamText as usual
 *
 * The returned object is structurally compatible with
 * `LanguageModelV1Middleware` (ai v3/v4) through `LanguageModelV4Middleware`
 * (ai v7) — only `wrapGenerate` / `wrapStream` are implemented, and the
 * stream-part and usage shapes of every major are handled.
 *
 * Interplay with the SDK's other capture layers (no double counting):
 * - Inside the module-level vercel-ai instrument (effective on ai v4 CJS)
 *   the call already runs in a dexcost suppression scope — the middleware
 *   detects that and passes through.
 * - The middleware wraps the provider's HTTP call in the same suppression
 *   scope, so the patched-fetch LLM fallback skips it (bytes are still
 *   counted into the task's network accountant).
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import {
  getCurrentTask,
  runWithTask,
  suppressNetworkEvent,
  isNetworkEventSuppressed,
} from "../core/context.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { getAmbientSessionTask } from "../core/session.js";
import { extractUsage } from "../instruments/ai-usage.js";
import type { ExtractedUsage } from "../instruments/ai-usage.js";
import { debugLog } from "../core/debug.js";
import type { CostTracker } from "../core/tracker.js";
import { getTracker } from "../core/tracker.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Options for {@link dexcostAiMiddleware}. */
export interface DexcostAiMiddlewareOptions {
  /**
   * Tracker to record into. Defaults to the singleton created by
   * `init()`, resolved lazily on each call — so the middleware can be
   * constructed at module scope, before `init()` has run.
   */
  tracker?: CostTracker;
  /**
   * Task type for auto-created tasks when no task/session is active.
   * Defaults to `"ai-sdk.generate"` / `"ai-sdk.stream"`.
   */
  taskType?: string;
}

/**
 * Structural middleware shape accepted by `wrapLanguageModel` across
 * ai v3–v7 (`LanguageModelV1Middleware` … `LanguageModelV4Middleware`).
 * Declared locally so `ai` / `@ai-sdk/provider` stay optional peers.
 */
export interface DexcostLanguageModelMiddleware {
  wrapGenerate: (opts: {
    doGenerate: () => PromiseLike<any>;
    params?: unknown;
    model?: any;
  }) => Promise<any>;
  wrapStream: (opts: {
    doStream: () => PromiseLike<any>;
    params?: unknown;
    model?: any;
  }) => Promise<any>;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

let _warnedNoTracker = false;

/** Test-only: reset the warn-once latch. */
export function _resetAiMiddlewareWarningsForTests(): void {
  _warnedNoTracker = false;
}

/**
 * Resolve the tracker for a call. Loud (once) when the SDK was never
 * initialized — a silent pass-through here would be exactly the invisible
 * failure mode this middleware exists to eliminate.
 */
function _resolveTracker(explicit?: CostTracker): CostTracker | null {
  if (explicit) return explicit;
  try {
    return getTracker();
  } catch {
    if (!_warnedNoTracker) {
      _warnedNoTracker = true;
      // eslint-disable-next-line no-console
      console.warn(
        "[dexcost] dexcostAiMiddleware is attached but dexcost is not " +
          "initialized (init() was never called) — AI SDK calls are passing " +
          "through untracked.",
      );
    }
    debugLog("ai-middleware", "skipped call: tracker not initialized");
    return null;
  }
}

function _modelId(model: any, params: any): string {
  if (typeof model?.modelId === "string" && model.modelId) return model.modelId;
  if (typeof params?.model === "string" && params.model) return params.model;
  return "unknown";
}

function _providerName(model: any): string {
  // Provider strings look like "anthropic.messages" / "openai.chat" —
  // keep the vendor segment so dashboard grouping matches the instruments.
  const raw = typeof model?.provider === "string" ? model.provider : "";
  const vendor = raw.split(".")[0];
  return vendor || "vercel-ai";
}

/**
 * Record one llm_call event and roll aggregates into the task.
 * Mirrors the vercel-ai instrument's recordEvent, but against an explicit
 * tracker instead of instrument-module state.
 */
function _recordEvent(
  tracker: CostTracker,
  task: Task,
  provider: string,
  model: string,
  usage: ExtractedUsage,
  latencyMs: number,
  source: "generate" | "stream",
): void {
  const { inputTokens, outputTokens, cachedTokens } = usage;
  const hasUsage = inputTokens > 0 || outputTokens > 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result = tracker.pricing.getCost(model, inputTokens, outputTokens, cachedTokens);
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
    inputTokens,
    outputTokens,
    cachedTokens,
    latencyMs,
    isRetry: false,
    details: { source: `ai_sdk_middleware_${source}` },
  });

  tracker.buffer.addEvent(event);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  task.totalCachedTokens += cachedTokens;
  tracker.buffer.upsertTask(task);

  debugLog(
    "ai-middleware",
    `llm_call captured via middleware (${source}): ${provider}/${model} ` +
      `in=${inputTokens} out=${outputTokens} cached=${cachedTokens}`,
  );
}

// ---------------------------------------------------------------------------
// Middleware factory
// ---------------------------------------------------------------------------

/**
 * Create a dexcost cost-tracking middleware for `wrapLanguageModel`.
 *
 * Failure posture: dexcost errors are contained (logged in debug mode,
 * never thrown into user code); user/provider errors are NEVER swallowed —
 * they propagate exactly as without the middleware, after the auto-task
 * is finalized as "failed".
 */
export function dexcostAiMiddleware(
  options: DexcostAiMiddlewareOptions = {},
): DexcostLanguageModelMiddleware {
  return {
    async wrapGenerate({ doGenerate, params, model }): Promise<any> {
      const tracker = _resolveTracker(options.tracker);
      if (!tracker) return doGenerate();

      if (isNetworkEventSuppressed()) {
        // An outer dexcost capture layer (module-level instrument, or a
        // second copy of this middleware) already owns this call.
        debugLog("ai-middleware", "skipped generate: outer dexcost capture active");
        return doGenerate();
      }

      let task = getCurrentTask();
      let autoCreated = false;
      if (!task) {
        // Join the ambient session (grouping with sibling HTTP/LLM calls
        // in the same context) when session tracking is active; the
        // session sweep owns its lifecycle. Otherwise fall back to a
        // per-call auto-task owned (and finalized) here.
        task = getAmbientSessionTask(options.taskType ?? "ai-sdk.generate");
        if (!task) {
          task = createAutoTask(options.taskType ?? "ai-sdk.generate");
          tracker.buffer.upsertTask(task);
          autoCreated = true;
        }
      }

      const startTime = performance.now();
      try {
        const result = await suppressNetworkEvent(() =>
          runWithTask(task!, () => doGenerate()),
        );
        try {
          const latencyMs = Math.round(performance.now() - startTime);
          _recordEvent(
            tracker,
            task,
            _providerName(model),
            _modelId(model, params),
            extractUsage(result?.usage),
            latencyMs,
            "generate",
          );
        } catch (err) {
          // dexcost errors must never crash user code — but never silently:
          debugLog("ai-middleware", `failed to record generate event: ${String(err)}`);
        }
        if (autoCreated) finalizeAutoTask(task, "success", tracker.buffer);
        return result;
      } catch (err) {
        if (autoCreated) finalizeAutoTask(task, "failed", tracker.buffer);
        throw err;
      }
    },

    async wrapStream({ doStream, params, model }): Promise<any> {
      const tracker = _resolveTracker(options.tracker);
      if (!tracker) return doStream();

      if (isNetworkEventSuppressed()) {
        debugLog("ai-middleware", "skipped stream: outer dexcost capture active");
        return doStream();
      }

      let task = getCurrentTask();
      let autoCreated = false;
      if (!task) {
        // Join the ambient session (grouping with sibling HTTP/LLM calls
        // in the same context) when session tracking is active; the
        // session sweep owns its lifecycle. Otherwise fall back to a
        // per-call auto-task owned (and finalized) here.
        task = getAmbientSessionTask(options.taskType ?? "ai-sdk.stream");
        if (!task) {
          task = createAutoTask(options.taskType ?? "ai-sdk.stream");
          tracker.buffer.upsertTask(task);
          autoCreated = true;
        }
      }

      const startTime = performance.now();
      let streamResult: any;
      try {
        streamResult = await suppressNetworkEvent(() =>
          runWithTask(task!, () => doStream()),
        );
      } catch (err) {
        if (autoCreated) finalizeAutoTask(task, "failed", tracker.buffer);
        throw err;
      }

      const stream: any = streamResult?.stream;
      if (!stream || typeof stream.getReader !== "function") {
        // Unexpected result shape (mock model, future spec change): we
        // cannot observe usage, but the auto-task must not leak as
        // "pending" and the anomaly must not be silent.
        debugLog(
          "ai-middleware",
          "stream result has no readable stream — usage not captured for this call",
        );
        if (autoCreated) finalizeAutoTask(task, "success", tracker.buffer);
        return streamResult;
      }

      // Watch the part stream for the terminal `finish` part (all spec
      // versions carry usage there), recording once on whichever terminal
      // signal arrives first: clean end, consumer cancel, or error.
      let usage: unknown;
      let sawFinish = false;
      let settled = false;
      const settle = (outcome: "end" | "cancel" | "error") => {
        if (settled) return;
        settled = true;
        try {
          if (sawFinish) {
            const latencyMs = Math.round(performance.now() - startTime);
            _recordEvent(
              tracker,
              task!,
              _providerName(model),
              _modelId(model, params),
              extractUsage(usage),
              latencyMs,
              "stream",
            );
          } else {
            debugLog(
              "ai-middleware",
              `stream ${outcome} before finish part — no usage available, no event recorded`,
            );
          }
        } catch (err) {
          debugLog("ai-middleware", `failed to record stream event: ${String(err)}`);
        }
        if (autoCreated) {
          const status = sawFinish || outcome === "cancel" ? "success" : "failed";
          finalizeAutoTask(task!, status, tracker.buffer);
        }
      };

      const reader = stream.getReader();
      const wrappedStream = new ReadableStream({
        async pull(controller) {
          let result: { done: boolean; value?: any };
          try {
            result = await reader.read();
          } catch (err) {
            settle("error");
            controller.error(err);
            return;
          }
          if (result.done) {
            settle("end");
            controller.close();
            return;
          }
          const part = result.value;
          try {
            if (part && part.type === "finish") {
              sawFinish = true;
              usage = part.usage ?? part.totalUsage;
            }
          } catch {
            // malformed part — pass through untouched
          }
          controller.enqueue(part);
        },
        cancel(reason) {
          settle("cancel");
          return reader.cancel(reason);
        },
      });

      return { ...streamResult, stream: wrappedStream };
    },
  };
}
