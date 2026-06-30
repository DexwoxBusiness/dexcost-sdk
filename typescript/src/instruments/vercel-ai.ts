/**
 * Vercel AI SDK auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `generateText` and `streamText` from the `ai` package to
 * automatically record cost events and aggregate token usage on the active
 * task context.
 *
 * The `ai` package is an optional peer dependency; the instrument gracefully
 * no-ops if the package is not installed.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask, runWithTask } from "../core/context.js";
import { createAutoTask } from "../core/auto-task.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalGenerateText: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalStreamText: Function | null = null;
let _aiModule: any = null;
/**
 * All distinct module objects that were patched (CJS and/or ESM).
 *
 * Unlike class-based SDKs (OpenAI, Anthropic, …) where patching a shared
 * prototype covers both module systems, the `ai` package exports standalone
 * functions.  `require("ai")` and `import("ai")` may return *different*
 * objects when the package ships dual CJS/ESM builds, so we must patch every
 * resolved module object to cover all consumers.
 */
let _patchedModules: any[] = [];
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock ai module so tests avoid importing ai. */
export function _setAiModule(mod: any): void {
  _aiModule = mod;
}

/** Test helper: reset to real module resolution. */
export function _resetAiModule(): void {
  _aiModule = null;
  _patchedModules = [];
}

/**
 * Extract the model identifier from Vercel AI SDK options.
 *
 * The `ai` package accepts model as either a string or an object with
 * a `modelId` property (e.g., `openai("gpt-4o")` returns an object).
 */
function extractModel(opts: any): string {
  if (typeof opts?.model === "string") return opts.model;
  if (opts?.model?.modelId) return String(opts.model.modelId);
  return "unknown";
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(
  model: string,
  inputTokens: number,
  outputTokens: number,
  task: Task,
  latencyMs: number,
): void {
  if (!_buffer || !_pricing) return;

  const hasUsage = inputTokens > 0 || outputTokens > 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result: CostResult = _pricing.getCost(model, inputTokens, outputTokens);
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
    provider: "vercel-ai",
    model,
    inputTokens,
    outputTokens,
    latencyMs,
    isRetry: false,
  });

  _buffer.addEvent(event);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  _buffer.upsertTask(task);
}

/**
 * Patch `generateText` and `streamText` from the `ai` module to record cost events.
 *
 * If `ai` is not installed and no mock module is injected, the dynamic
 * import will throw and the function will reject.
 *
 * ## CJS / ESM dual-patch
 *
 * The `ai` package exports standalone functions (not class prototypes), so
 * monkey-patching is done on the module namespace object.  In Node.js,
 * `require("ai")` (CJS) and `import("ai")` (ESM) can return *different*
 * objects when the package ships dual builds.  If we only patch the ESM
 * namespace, CJS consumers (NestJS, tsc-compiled apps, NX workspaces) call
 * the **original** unpatched function — silently losing all cost data.
 *
 * Fix: resolve the module via both CJS `require` and ESM `import`, then
 * patch every distinct object so both consumer types hit the instrumented
 * wrappers.
 */
export async function instrumentVercelAi(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  if (!_aiModule) {
    // Collect every distinct module object we can resolve.
    const modules: any[] = [];

    // 1. CJS path — covers NestJS, tsc-compiled, and other CJS apps.
    //    `typeof require` is safe on undeclared identifiers and evaluates
    //    to "undefined" in pure ESM contexts without throwing.
    // @ts-ignore -- require is only available in CJS context
    if (typeof require === "function") {
      try {
        // @ts-ignore -- require is only available in CJS context
        modules.push(require("ai"));
      } catch {
        // CJS resolution failed (ESM-only package, or not installed)
      }
    }

    // 2. ESM path — covers native ESM apps.
    try {
      // @ts-ignore -- ai is an optional peer dependency
      const esmMod = await import("ai");
      // Only add if it is a genuinely different object (same package
      // may resolve to the same object in some bundler setups).
      if (!modules.includes(esmMod)) modules.push(esmMod);
    } catch {
      // ESM resolution failed (not installed)
    }

    if (modules.length === 0) {
      throw new Error(
        "Cannot find package 'ai' — install it to enable Vercel AI SDK instrumentation",
      );
    }

    // Use the first (preferred CJS) module as canonical reference for
    // saving originals and for the test helpers.
    _aiModule = modules[0];
    _patchedModules = modules;
  } else {
    // Test-injected mock — treat as the only module.
    _patchedModules = [_aiModule];
  }

  _originalGenerateText = _aiModule.generateText;
  _originalStreamText = _aiModule.streamText;
  _buffer = buffer;
  _pricing = pricing;

  // Build the patched functions once, then assign to all module objects.
  const patchedGenerateText = async function patchedGenerateText(
    this: any,
    opts: any,
  ): Promise<any> {
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("vercel-ai.generateText");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    const self = this;
    try {
      const result = await runWithTask(task, () =>
        _originalGenerateText!.call(self, opts),
      );
      const latencyMs = Math.round(performance.now() - startTime);

      const model = extractModel(opts);
      const inputTokens: number = result?.usage?.promptTokens ?? 0;
      const outputTokens: number = result?.usage?.completionTokens ?? 0;

      recordEvent(model, inputTokens, outputTokens, task, latencyMs);
      if (autoCreated) {
        task.status = "success";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
      }
      return result;
    } catch (err) {
      if (autoCreated) {
        task.status = "failed";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
      }
      throw err;
    }
  };

  const patchedStreamText = function patchedStreamText(
    this: any,
    opts: any,
  ): any {
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("vercel-ai.streamText");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    const self = this;
    const streamResult = runWithTask(task, () =>
      _originalStreamText!.call(self, opts),
    );

    // Wrap the stream to capture usage after iteration completes.
    // The Vercel AI SDK streamText returns an object with an async iterator
    // for text chunks and a `usage` promise that resolves when done.
    if (streamResult && typeof streamResult[Symbol.asyncIterator] === "function") {
      return wrapStream(streamResult, opts, task, startTime, autoCreated);
    }

    return streamResult;
  };

  // Apply patches to ALL resolved module objects (CJS + ESM).
  // ESM module namespace objects have read-only properties (per spec), so
  // assigning to them throws in strict mode.  We attempt each assignment
  // inside a try/catch: if a module is immutable we skip it rather than
  // leaving the instrument in a half-patched state.
  const successfullyPatched: any[] = [];
  for (const mod of _patchedModules) {
    try {
      mod.generateText = patchedGenerateText;
      mod.streamText = patchedStreamText;
      successfullyPatched.push(mod);
    } catch {
      // Module namespace is frozen/sealed (typical for pure ESM namespace
      // objects).  The CJS exports object — which is the one CJS consumers
      // actually call — is always writable, so this is safe to skip.
    }
  }

  if (successfullyPatched.length === 0) {
    // Roll back state so a future retry starts clean.
    _aiModule = null;
    _patchedModules = [];
    throw new Error(
      "Could not patch 'ai' module — all resolved objects are read-only",
    );
  }

  _patchedModules = successfullyPatched;
  _patched = true;
}

/**
 * Remove the monkey-patches and restore the original functions on ALL
 * module objects that were patched (CJS and/or ESM).
 */
export function uninstrumentVercelAi(): void {
  if (!_patched) return;

  for (const mod of _patchedModules) {
    try {
      if (_originalGenerateText) mod.generateText = _originalGenerateText;
      if (_originalStreamText) mod.streamText = _originalStreamText;
    } catch {
      // Module became frozen between instrument and uninstrument — unusual
      // but harmless; the module will be GC'd with the patched wrappers.
    }
  }

  _originalGenerateText = null;
  _originalStreamText = null;
  _patchedModules = [];
  _buffer = null;
  _pricing = null;
  _patched = false;
}

function wrapStream(
  rawStream: any,
  opts: any,
  task: Task,
  startTime: number,
  autoCreated: boolean = false,
): AsyncIterable<any> & Record<string, any> {
  let recorded = false;

  // Preserve all properties of the original stream result
  const wrapped: any = Object.create(rawStream);

  wrapped[Symbol.asyncIterator] = function () {
    const iter = rawStream[Symbol.asyncIterator]();
    return {
      async next(): Promise<IteratorResult<any>> {
        let result: IteratorResult<any>;
        try {
          result = await iter.next();
        } catch (err) {
          if (autoCreated && _buffer) {
            task.status = "failed";
            task.endedAt = new Date();
            _buffer.upsertTask(task);
          }
          throw err;
        }
        if (result.done && !recorded) {
          recorded = true;
          const latencyMs = Math.round(performance.now() - startTime);

          // After stream completes, try to read usage from the stream result.
          // Vercel AI SDK exposes usage as a property or promise on the result.
          let inputTokens = 0;
          let outputTokens = 0;

          try {
            const usage = rawStream.usage ?? (await rawStream.usage);
            if (usage) {
              inputTokens = usage.promptTokens ?? 0;
              outputTokens = usage.completionTokens ?? 0;
            }
          } catch {
            // usage not available
          }

          const model = extractModel(opts);
          recordEvent(model, inputTokens, outputTokens, task, latencyMs);
          if (autoCreated && _buffer) {
            task.status = "success";
            task.endedAt = new Date();
            _buffer.upsertTask(task);
          }
        }
        return result;
      },
    };
  };

  return wrapped;
}
// Self-register so importing this module is enough to make the instrument available.
registerInstrument("vercel-ai", instrumentVercelAi, uninstrumentVercelAi);
