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
import { createRequire } from "node:module";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask, runWithTask, suppressNetworkEvent } from "../core/context.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { getAmbientSessionTask } from "../core/session.js";
import { extractUsage } from "./ai-usage.js";
import type { ExtractedUsage } from "./ai-usage.js";
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

// Usage extraction is shared with the model-level middleware
// (integrations/ai-sdk.ts) — see instruments/ai-usage.ts.

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(
  model: string,
  usage: ExtractedUsage,
  task: Task,
  latencyMs: number,
): void {
  if (!_buffer || !_pricing) return;

  const { inputTokens, outputTokens, cachedTokens } = usage;
  const hasUsage = inputTokens > 0 || outputTokens > 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result: CostResult = _pricing.getCost(
      model,
      inputTokens,
      outputTokens,
      cachedTokens,
    );
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
    cachedTokens,
    latencyMs,
    isRetry: false,
  });

  _buffer.addEvent(event);
  registerLlmCapture(task.taskId, inputTokens, outputTokens);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  task.totalCachedTokens += cachedTokens;
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
    //    `createRequire` works from BOTH builds of this SDK (the CJS build
    //    shims `import.meta.url`), unlike a bare `require` reference which
    //    is undefined in the ESM build and silently skipped this path.
    //    Note: `ai` >= 5 ships ESM-only; on Node >= 20.19 `require(esm)`
    //    returns the (unpatchable) module namespace, which the
    //    effectiveness check below rejects; on older Node it throws
    //    ERR_REQUIRE_ESM and is caught here. `ai` v4 resolves to a mutable
    //    CJS exports object and patches cleanly.
    try {
      const cjsRequire = createRequire(import.meta.url);
      modules.push(cjsRequire("ai"));
    } catch {
      // CJS resolution failed (ESM-only package, or not installed)
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
      // Join the ambient session (grouping with sibling HTTP/LLM calls
      // in the same context) when session tracking is active; the
      // session sweep owns its lifecycle. Otherwise fall back to a
      // per-call auto-task owned (and finalized) here.
      task = getAmbientSessionTask("vercel-ai.generateText");
      if (!task) {
        task = createAutoTask("vercel-ai.generateText");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const self = this;
    try {
      const result = await suppressNetworkEvent(() =>
        runWithTask(task, () => _originalGenerateText!.call(self, opts)),
      );
      const latencyMs = Math.round(performance.now() - startTime);

      const model = extractModel(opts);
      // v5+ multi-step calls (tool loops) report the aggregate in
      // `totalUsage`; `usage` covers only the final step.
      const usage = extractUsage(result?.totalUsage ?? result?.usage);

      recordEvent(model, usage, task, latencyMs);
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return result;
    } catch (err) {
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    }
  };

  // NOTE: streamText is SYNCHRONOUS in the Vercel AI SDK — it returns a
  // StreamTextResult immediately (unlike generateText, which is async). The
  // wrapper must preserve that contract and return the (wrapped) stream
  // synchronously; making it async would return a Promise and break callers
  // that do `const result = streamText(...)` without awaiting.
  const patchedStreamText = function patchedStreamText(
    this: any,
    opts: any,
  ): any {
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      // Join the ambient session (grouping with sibling HTTP/LLM calls
      // in the same context) when session tracking is active; the
      // session sweep owns its lifecycle. Otherwise fall back to a
      // per-call auto-task owned (and finalized) here.
      task = getAmbientSessionTask("vercel-ai.streamText");
      if (!task) {
        task = createAutoTask("vercel-ai.streamText");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const self = this;
    try {
      const streamResult = suppressNetworkEvent(() =>
        runWithTask(task, () => _originalStreamText!.call(self, opts)),
      );

      // The real Vercel AI SDK `StreamTextResult` is NOT async-iterable —
      // callers consume `result.textStream` / `result.fullStream`. The
      // reliable capture point is the `usage` promise (v5+: `totalUsage`
      // aggregates multi-step tool loops), which resolves once the stream
      // finishes and never forces consumption. Recording through it also
      // returns the ORIGINAL result object untouched, so instanceof checks
      // and lazy getters on StreamTextResult keep working.
      const usagePromise = streamResult?.totalUsage ?? streamResult?.usage;
      if (usagePromise && typeof usagePromise.then === "function") {
        let recorded = false;
        Promise.resolve(usagePromise).then(
          (usage: any) => {
            if (recorded) return;
            recorded = true;
            try {
              const latencyMs = Math.round(performance.now() - startTime);
              recordEvent(extractModel(opts), extractUsage(usage), task, latencyMs);
            } catch {
              // dexcost errors must never crash user code
            }
            if (autoCreated) {
              finalizeAutoTask(task, "success", _buffer);
            }
          },
          () => {
            // Stream errored or was aborted before usage was known.
            if (recorded) return;
            recorded = true;
            if (autoCreated) {
              finalizeAutoTask(task, "failed", _buffer);
            }
          },
        );
        return streamResult;
      }

      // Legacy/mock shape: the result itself is async-iterable — wrap the
      // iterator to capture usage after iteration completes.
      if (streamResult && typeof streamResult[Symbol.asyncIterator] === "function") {
        return wrapStream(streamResult, opts, task, startTime, autoCreated);
      }

      // Non-stream fallback: nothing to wrap or await. Finalize the
      // auto-created task here so it is not left "pending" forever. Guard
      // matches wrapStream's finalizeTask (autoCreated && _buffer).
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return streamResult;
    } catch (err) {
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    }
  };

  // Apply patches to ALL resolved module objects (CJS + ESM).
  // ESM module namespace objects have read-only properties (per spec):
  // assigning to them throws in strict mode and is SILENTLY IGNORED in
  // sloppy mode — the assignment "succeeds" but the export still points at
  // the original function. So a try/catch alone is not enough; after each
  // assignment we read the property back and only count the module as
  // patched when the write actually landed. Without this check the
  // instrument reports success on `ai` >= 5 (ESM-only) while capturing
  // nothing, and — worse — the HTTP-fallback path assumes the instrument
  // has the call covered.
  const successfullyPatched: any[] = [];
  for (const mod of _patchedModules) {
    try {
      mod.generateText = patchedGenerateText;
      mod.streamText = patchedStreamText;
      if (
        mod.generateText === patchedGenerateText &&
        mod.streamText === patchedStreamText
      ) {
        successfullyPatched.push(mod);
      }
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
      "Could not patch the 'ai' module: its exports are read-only ES module " +
        "namespaces ('ai' >= 5 ships ESM-only builds, which cannot be " +
        "monkey-patched). Vercel AI SDK calls are still captured at the " +
        "HTTP layer when trackHttp is enabled (the default).",
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
    const finalizeTask = (status: "success" | "failed") => {
      if (recorded) return;
      recorded = true;
      if (autoCreated) {
        finalizeAutoTask(task, status, _buffer);
      }
    };
    return {
      async next(): Promise<IteratorResult<any>> {
        let result: IteratorResult<any>;
        try {
          result = await iter.next();
        } catch (err) {
          finalizeTask("failed");
          throw err;
        }
        if (result.done && !recorded) {
          recorded = true;
          const latencyMs = Math.round(performance.now() - startTime);

          // After stream completes, try to read usage from the stream result.
          // Vercel AI SDK exposes usage as a property or promise on the result.
          let usage: ExtractedUsage = { inputTokens: 0, outputTokens: 0, cachedTokens: 0 };

          try {
            usage = extractUsage(await (rawStream.totalUsage ?? rawStream.usage));
          } catch {
            // usage not available
          }

          const model = extractModel(opts);
          recordEvent(model, usage, task, latencyMs);
          if (autoCreated) {
            finalizeAutoTask(task, "success", _buffer);
          }
        }
        return result;
      },
      async return(value?: any): Promise<IteratorResult<any>> {
        finalizeTask("success");
        return iter.return ? await iter.return(value) : { done: true as const, value };
      },
      async throw(error?: any): Promise<IteratorResult<any>> {
        finalizeTask("failed");
        if (iter.throw) return await iter.throw(error);
        throw error;
      },
    };
  };

  return wrapped;
}

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("vercel-ai", instrumentVercelAi, uninstrumentVercelAi, (ref: any) => {
  _setAiModule(ref?.default ?? ref);
});
