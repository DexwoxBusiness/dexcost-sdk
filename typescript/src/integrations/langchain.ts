/**
 * DexcostCallbackHandler — duck-typed LangChain callback handler.
 *
 * Works with any LangChain version without importing `@langchain/core`.
 * Attach to a LangChain chain/agent as a callback to automatically track
 * LLM call costs inside dexcost tasks.
 *
 * Usage:
 *   const handler = new DexcostCallbackHandler(tracker);
 *   const chain = new LLMChain({ llm, prompt, callbacks: [handler] });
 */

import { randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent } from "../core/models.js";
import type { PricingEngine } from "../pricing/engine.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { CostTracker } from "../core/tracker.js";

// ---------------------------------------------------------------------------
// Internal pending-run state
// ---------------------------------------------------------------------------

interface PendingRun {
  model: string;
  startTime: number;
}

// ---------------------------------------------------------------------------
// DexcostCallbackHandler
// ---------------------------------------------------------------------------

/**
 * A duck-typed LangChain callback handler that records cost events for every
 * LLM call that occurs inside a dexcost tracked task.
 *
 * No `@langchain/core` dependency — the handler implements the subset of the
 * BaseCallbackHandler interface that LangChain calls for LLM lifecycle events.
 */
export class DexcostCallbackHandler {
  private _pricing: PricingEngine;
  private _buffer: EventBuffer;
  private _pending: Map<string, PendingRun> = new Map();

  constructor(tracker: CostTracker) {
    this._pricing = tracker.pricing;
    this._buffer = tracker.buffer;
  }

  /**
   * Called by LangChain at the start of an LLM invocation.
   *
   * @param serialized - Serialized LLM object. Model name is extracted from
   *   `serialized.kwargs.model_name` (preferred) or the last element of
   *   `serialized.id` (fallback).
   * @param prompts - The input prompt strings (unused beyond signalling start).
   * @param runId - Unique identifier for this run, used to correlate start/end.
   */
  handleLLMStart(
    serialized: Record<string, unknown>,
    _prompts: string[],
    runId: string,
  ): void {
    const model = this._extractModel(serialized);
    this._pending.set(runId, { model, startTime: Date.now() });
  }

  /**
   * Called by LangChain when an LLM invocation completes successfully.
   *
   * Records a `llm_call` cost event into the active task (if any).
   *
   * @param output - LLM output object. Token counts are read from
   *   `output.llmOutput.tokenUsage.{promptTokens, completionTokens}`.
   * @param runId - Matches the runId from the corresponding handleLLMStart.
   */
  handleLLMEnd(output: Record<string, unknown>, runId: string): void {
    const pending = this._pending.get(runId);
    if (!pending) return;

    const { model, startTime } = pending;
    const latencyMs = Date.now() - startTime;

    // Extract token counts from output.llmOutput.tokenUsage
    const llmOutput = output["llmOutput"] as Record<string, unknown> | undefined;
    const tokenUsage = llmOutput?.["tokenUsage"] as Record<string, unknown> | undefined;
    const promptTokens = (tokenUsage?.["promptTokens"] as number | undefined) ?? 0;
    const completionTokens = (tokenUsage?.["completionTokens"] as number | undefined) ?? 0;

    // Requires an active task context
    const task = getCurrentTask();
    if (!task) {
      this._pending.delete(runId);
      return;
    }

    // Compute cost
    const costResult = this._pricing.getCost(model, promptTokens, completionTokens);

    // Build event
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: task.taskId,
      eventType: "llm_call",
      provider: "langchain",
      model,
      inputTokens: promptTokens,
      outputTokens: completionTokens,
      costUsd: costResult.costUsd,
      costConfidence: costResult.costConfidence,
      pricingSource: costResult.pricingSource,
      latencyMs,
      isRetry: false,
    });

    // Persist event
    this._buffer.addEvent(event);

    // Update task aggregates
    task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
    task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
    task.totalInputTokens += promptTokens;
    task.totalOutputTokens += completionTokens;

    this._buffer.upsertTask(task);

    this._pending.delete(runId);
  }

  /**
   * Called by LangChain when an LLM invocation fails.
   *
   * Records a failure `llm_call` event (cost 0, `error_type` in details)
   * when a task context is active, mirroring the Python SDK's
   * `on_llm_error`. Never throws.
   *
   * @param error - The error that occurred.
   * @param runId - Matches the runId from the corresponding handleLLMStart.
   */
  handleLLMError(error: Error, runId: string): void {
    const pending = this._pending.get(runId);
    this._pending.delete(runId);

    const task = getCurrentTask();
    if (!task) {
      // No active task — nothing to attribute the failure to.
      return;
    }

    const model = pending?.model ?? "unknown";
    const latencyMs = pending ? Date.now() - pending.startTime : undefined;
    const errorType = error?.name ?? "Error";

    try {
      const event = createCostEvent({
        eventId: randomUUID(),
        taskId: task.taskId,
        eventType: "llm_call",
        costUsd: 0,
        costConfidence: "unknown",
        pricingSource: "unknown",
        provider: "langchain",
        model,
        latencyMs,
        isRetry: false,
        details: { error: String(error?.message ?? error), error_type: errorType },
      });
      this._buffer.addEvent(event);
      this._buffer.upsertTask(task);
    } catch {
      // dexcost errors must never crash user code
    }
  }

  // ---------------------------------------------------------------------------
  // Private helpers
  // ---------------------------------------------------------------------------

  /**
   * Extract the model identifier from a serialized LangChain LLM object.
   *
   * Preference order:
   *   1. `serialized.kwargs.model_name` — set by most ChatModel subclasses
   *   2. last element of `serialized.id` — the class name (e.g. "ChatOpenAI")
   */
  private _extractModel(serialized: Record<string, unknown>): string {
    const kwargs = serialized["kwargs"] as Record<string, unknown> | undefined;
    if (kwargs?.["model_name"] && typeof kwargs["model_name"] === "string") {
      return kwargs["model_name"];
    }

    const id = serialized["id"];
    if (Array.isArray(id) && id.length > 0) {
      const last = id[id.length - 1];
      if (typeof last === "string") return last;
    }

    return "unknown";
  }
}
