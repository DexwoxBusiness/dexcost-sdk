/**
 * Thin wrapper classes around the OpenAI and Anthropic clients that
 * auto-record LLM cost events.
 *
 * Alternative to auto-instrumentation (no monkey-patching). Matching
 * the Python SDK's clients.py.
 *
 * Usage:
 *
 *   import { TrackedOpenAI } from "@dexcost/sdk/clients";
 *
 *   const client = new TrackedOpenAI({ tracker });
 *   // Inside a tracked task, events are auto-recorded:
 *   const response = await client.chat.completions.create({ model: "gpt-4o", messages: [] });
 */

import { randomUUID } from "node:crypto";
import { getCurrentTask } from "./core/context.js";
import { createCostEvent, Decimal } from "./core/models.js";
import { createAutoTask } from "./core/auto-task.js";
import { registerLlmCapture } from "./core/llm-dedup.js";
import type { Task, CostConfidence, PricingSource } from "./core/models.js";
import { PricingEngine } from "./pricing/engine.js";
import type { EventBuffer } from "./transport/buffer.js";
import type { CostTracker } from "./core/tracker.js";

// ---------------------------------------------------------------------------
// TrackedOpenAI
// ---------------------------------------------------------------------------

/**
 * Wraps `openai.OpenAI` and auto-records llm_call events on every
 * `chat.completions.create` call when running inside a tracked task context.
 */
export class TrackedOpenAI {
  private _client: unknown;
  private _pricing: PricingEngine;
  private _buffer: EventBuffer | null;

  constructor(opts?: { client?: unknown; tracker?: CostTracker; pricing?: PricingEngine }) {
    if (opts?.client !== undefined) {
      this._client = opts.client;
    } else {
      // Lazy require — openai is an optional peer dependency
      let OpenAIClass: new () => unknown;
      try {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        const openaiModule = require("openai") as { default?: { new(): unknown }; OpenAI?: { new(): unknown } };
        OpenAIClass = (openaiModule.default ?? openaiModule.OpenAI) as typeof OpenAIClass;
      } catch {
        throw new Error(
          "The 'openai' package is required for TrackedOpenAI. Install it with: npm install openai"
        );
      }
      this._client = new OpenAIClass();
    }

    if (opts?.tracker !== undefined) {
      this._pricing = opts.tracker.pricing;
      this._buffer = opts.tracker.buffer;
    } else if (opts?.pricing !== undefined) {
      this._pricing = opts.pricing;
      this._buffer = null;
    } else {
      this._pricing = new PricingEngine();
      this._buffer = null;
    }
  }

  get chat(): { completions: { create: (...args: unknown[]) => Promise<unknown> } } {
    const pricing = this._pricing;
    const buffer = this._buffer;
    const client = this._client as {
      chat: { completions: { create: (...args: unknown[]) => Promise<unknown> } };
    };

    return {
      completions: {
        create: async (...args: unknown[]): Promise<unknown> => {
          const response = await client.chat.completions.create(...args);
          recordOpenAIEvent(response, args[0], pricing, buffer);
          return response;
        },
      },
    };
  }
}

function recordOpenAIEvent(
  response: unknown,
  body: unknown,
  pricing: PricingEngine,
  buffer: EventBuffer | null,
): void {
  if (!buffer) return;

  let task = getCurrentTask();
  if (!task) {
    // Auto-create a task so LLM costs are never silently lost
    // (mirrors Python create_auto_task).
    task = createAutoTask("openai.chat");
    buffer.upsertTask(task);
  }

  const resp = response as Record<string, unknown>;
  const bodyObj = (body ?? {}) as Record<string, unknown>;

  const model: string =
    (resp["model"] as string | undefined) ??
    (bodyObj["model"] as string | undefined) ??
    "unknown";

  const usage = resp["usage"] as Record<string, unknown> | undefined;
  const hasUsage = usage != null;

  const inputTokens: number = (usage?.["prompt_tokens"] as number | undefined) ?? 0;
  const outputTokens: number = (usage?.["completion_tokens"] as number | undefined) ?? 0;
  const promptDetails = usage?.["prompt_tokens_details"] as Record<string, unknown> | undefined;
  const cachedTokens: number = (promptDetails?.["cached_tokens"] as number | undefined) ?? 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result = pricing.getCost(model, inputTokens, outputTokens, cachedTokens);
    costUsd = result.costUsd;
    costConfidence = result.costConfidence;
    pricingSource = result.pricingSource;
  }

  _addEventAndUpdateTask(task, buffer, {
    provider: "openai",
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    costUsd,
    costConfidence,
    pricingSource,
  });
}

// ---------------------------------------------------------------------------
// TrackedAnthropic
// ---------------------------------------------------------------------------

/**
 * Wraps `@anthropic-ai/sdk Anthropic` and auto-records llm_call events on
 * every `messages.create` call when running inside a tracked task context.
 */
export class TrackedAnthropic {
  private _client: unknown;
  private _pricing: PricingEngine;
  private _buffer: EventBuffer | null;

  constructor(opts?: { client?: unknown; tracker?: CostTracker; pricing?: PricingEngine }) {
    if (opts?.client !== undefined) {
      this._client = opts.client;
    } else {
      // Lazy require — @anthropic-ai/sdk is an optional peer dependency
      let AnthropicClass: new () => unknown;
      try {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        const anthropicModule = require("@anthropic-ai/sdk") as {
          default?: { new(): unknown };
          Anthropic?: { new(): unknown };
        };
        AnthropicClass = (anthropicModule.default ?? anthropicModule.Anthropic) as typeof AnthropicClass;
      } catch {
        throw new Error(
          "The '@anthropic-ai/sdk' package is required for TrackedAnthropic. Install it with: npm install @anthropic-ai/sdk"
        );
      }
      this._client = new AnthropicClass();
    }

    if (opts?.tracker !== undefined) {
      this._pricing = opts.tracker.pricing;
      this._buffer = opts.tracker.buffer;
    } else if (opts?.pricing !== undefined) {
      this._pricing = opts.pricing;
      this._buffer = null;
    } else {
      this._pricing = new PricingEngine();
      this._buffer = null;
    }
  }

  get messages(): { create: (...args: unknown[]) => Promise<unknown> } {
    const pricing = this._pricing;
    const buffer = this._buffer;
    const client = this._client as {
      messages: { create: (...args: unknown[]) => Promise<unknown> };
    };

    return {
      create: async (...args: unknown[]): Promise<unknown> => {
        const response = await client.messages.create(...args);
        recordAnthropicEvent(response, args[0], pricing, buffer);
        return response;
      },
    };
  }
}

function recordAnthropicEvent(
  response: unknown,
  body: unknown,
  pricing: PricingEngine,
  buffer: EventBuffer | null,
): void {
  if (!buffer) return;

  let task = getCurrentTask();
  if (!task) {
    // Auto-create a task so LLM costs are never silently lost
    // (mirrors Python create_auto_task).
    task = createAutoTask("anthropic.messages");
    buffer.upsertTask(task);
  }

  const resp = response as Record<string, unknown>;
  const bodyObj = (body ?? {}) as Record<string, unknown>;

  const model: string =
    (resp["model"] as string | undefined) ??
    (bodyObj["model"] as string | undefined) ??
    "unknown";

  const usage = resp["usage"] as Record<string, unknown> | undefined;
  const hasUsage = usage != null;

  const inputTokens: number = (usage?.["input_tokens"] as number | undefined) ?? 0;
  const outputTokens: number = (usage?.["output_tokens"] as number | undefined) ?? 0;
  const cacheCreationTokens: number =
    (usage?.["cache_creation_input_tokens"] as number | undefined) ?? 0;
  const cacheReadTokens: number =
    (usage?.["cache_read_input_tokens"] as number | undefined) ?? 0;
  // cachedTokens tracks cache_read (for aggregation); cache_creation stored in details
  const cachedTokens: number = cacheReadTokens;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result = pricing.getCost(
      model,
      inputTokens,
      outputTokens,
      cacheReadTokens,
      cacheCreationTokens,
    );
    costUsd = result.costUsd;
    costConfidence = result.costConfidence;
    pricingSource = result.pricingSource;
  }

  _addEventAndUpdateTask(task, buffer, {
    provider: "anthropic",
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    costUsd,
    costConfidence,
    pricingSource,
    details: cacheCreationTokens > 0 ? { cache_creation_input_tokens: cacheCreationTokens } : undefined,
  });
}

// ---------------------------------------------------------------------------
// Shared helper
// ---------------------------------------------------------------------------

interface EventSpec {
  provider: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
  costUsd: Decimal;
  costConfidence: CostConfidence;
  pricingSource: PricingSource;
  details?: Record<string, unknown>;
}

function _addEventAndUpdateTask(
  task: Task,
  buffer: EventBuffer,
  spec: EventSpec,
): void {
  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "llm_call",
    costUsd: spec.costUsd,
    costConfidence: spec.costConfidence,
    pricingSource: spec.pricingSource,
    provider: spec.provider,
    model: spec.model,
    inputTokens: spec.inputTokens,
    outputTokens: spec.outputTokens,
    cachedTokens: spec.cachedTokens,
    isRetry: false,
    details: spec.details ?? {},
  });

  buffer.addEvent(event);
  registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);

  task.llmCostUsd = task.llmCostUsd.plus(spec.costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(spec.costUsd);
  task.totalInputTokens += spec.inputTokens;
  task.totalOutputTokens += spec.outputTokens;
  task.totalCachedTokens += spec.cachedTokens;

  buffer.upsertTask(task);
}

// ---------------------------------------------------------------------------
// wrap* convention entry points
// ---------------------------------------------------------------------------

/**
 * Wrap an OpenAI client instance for cost tracking (ecosystem `wrapOpenAI`
 * convention). Returns a {@link TrackedOpenAI} exposing the chat-completions
 * surface; for FULL client-surface coverage prefer injecting a tracked
 * fetch instead: `new OpenAI({ fetch: createDexcostFetch() })`.
 */
export function wrapOpenAI(
  client: unknown,
  opts?: { tracker?: CostTracker },
): TrackedOpenAI {
  return new TrackedOpenAI({ client, tracker: opts?.tracker });
}

/**
 * Wrap an Anthropic client instance for cost tracking (ecosystem
 * `wrapAnthropic` convention). Returns a {@link TrackedAnthropic} exposing
 * the messages surface; for FULL client-surface coverage prefer injecting
 * a tracked fetch instead: `new Anthropic({ fetch: createDexcostFetch() })`.
 */
export function wrapAnthropic(
  client: unknown,
  opts?: { tracker?: CostTracker },
): TrackedAnthropic {
  return new TrackedAnthropic({ client, tracker: opts?.tracker });
}
