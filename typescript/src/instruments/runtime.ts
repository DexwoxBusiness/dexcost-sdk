/** Explicit runtime allocation evidence. No provider tariffs or guessed lifetime. */
import { randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent } from "../core/models.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";

export interface RuntimeResource {
  serviceKey: "modal_compute" | "e2b_sandbox";
  billingAccountId: string;
  resourceId: string;
  vcpuCount?: number;
  memoryMiB?: number;
}
const active = new AsyncLocalStorage<ReadonlySet<string>>();
function configuration(config: RuntimeResource): string {
  if (!["modal_compute", "e2b_sandbox"].includes(config.serviceKey)) throw new Error("Unsupported runtime");
  for (const value of [config.vcpuCount, config.memoryMiB]) {
    if (value !== undefined && (!Number.isInteger(value) || value < 1 || value > 1_048_576)) {
      throw new Error("Resource configuration must be a positive integer");
    }
  }
  return databaseResourceId(config.billingAccountId, config.resourceId);
}

/** Elapsed client work is an allocation weight, not provider-billed runtime.
 * Use instead of the legacy GPU monetary wrapper. Preserves sync return values.
 */
export function wrapRuntimeHandler<A extends unknown[], R>(
  fn: (...args: A) => R, tracker: CostTracker, config: RuntimeResource,
): (...args: A) => R {
  config = Object.freeze({ ...config });
  const resource = configuration(config);
  return function(this: unknown, ...args: A): R {
    const task = getCurrentTask();
    const key = `${config.serviceKey}/${resource}/${task?.taskId}`;
    if (!task || active.getStore()?.has(key)) return fn.apply(this, args);
    const started = new Date(), clock = performance.now();
    let finished = false;
    const finish = (failed: boolean): void => {
      if (finished) return;
      finished = true;
      try {
        const milliseconds = Math.floor(performance.now() - clock);
        if (!Number.isFinite(milliseconds) || milliseconds <= 0 || milliseconds > 86_400_000) return;
        const seconds = `${Math.floor(milliseconds / 1000)}.${String(milliseconds % 1000).padStart(3, "0")}`;
        const dimensions = [
          ["runtime.vcpu_count", config.vcpuCount], ["runtime.memory_mib", config.memoryMiB],
        ].filter(([, v]) => v !== undefined).map(([k, v]) => ({ key: k, value: { type: "integer", value: String(v) } }));
        tracker.buffer.addEvent(createCostEvent({
          eventId: randomUUID(), taskId: task.taskId, occurredAt: new Date(started.getTime() + milliseconds),
          eventType: "external_cost", costConfidence: "unknown", provider: config.serviceKey,
          serviceName: "runtime", latencyMs: milliseconds,
          details: {
            attribution_component: "compute", attribution_resource_type: "instance", attribution_resource_id: resource,
            attribution_operation_name: "runtime.work", attribution_operation_status: failed ? "failed" : "succeeded",
            attribution_usage_duration_seconds: seconds,
            attribution_usage_lines: [{ metric: "runtime.task_seconds", unit: "Seconds", quantity: seconds }],
            attribution_dimensions: dimensions, runtime_capture_basis: "observed_task_wall_time_not_billed_runtime",
          },
        }));
      } catch { /* Capture must not change results or expose payloads. */ }
    };
    return active.run(new Set([...(active.getStore() ?? []), key]), () => {
      try {
        const result = fn.apply(this, args);
        if (result && typeof (result as any).next === "function") return result;
        if (result && typeof (result as any).then === "function") {
          return Promise.resolve(result).then(value => {
            if (!value || typeof (value as any).next !== "function") finish(false);
            return value;
          },
            (error: unknown) => { finish(true); throw error; }) as R;
        }
        finish(false);
        return result;
      } catch (error) { finish(true); throw error; }
    });
  };
}

const facades = new WeakSet<object>();
const captures = new WeakSet<object>();
/** Use the returned facade. Only completed commands.run/runCode calls are timed.
 * Background calls and unobserved idle time remain unallocated. No extra API calls.
 */
export function instrumentE2bSandbox<T extends { sandboxId: string }>(
  sandbox: T, tracker: CostTracker, config: Omit<RuntimeResource, "serviceKey" | "resourceId">,
): { sandbox: T; close(): void } {
  if (facades.has(sandbox)) throw new Error("Sandbox is already instrumented");
  const resource = { ...config, serviceKey: "e2b_sandbox" as const, resourceId: sandbox.sandboxId };
  configuration(resource);
  let closed = false;
  function facade(target: any, commands = false): any {
    return new Proxy(Object.create(Object.getPrototypeOf(target)), {
      get(_target, property) {
        const value = Reflect.get(target, property, target);
        if (!commands && property === "commands") return facade(value, true);
        if (typeof value !== "function") return value;
        const bound = value.bind(target);
        if ((commands && property === "run") || (!commands && property === "runCode")) {
          const wrapped = wrapRuntimeHandler(bound, tracker, resource);
          return (...args: any[]) => closed || args[1]?.background ? bound(...args) : wrapped(...args);
        }
        return bound;
      },
      set(_target, property, value) { return Reflect.set(target, property, value, target); },
    });
  }
  const proxy = facade(sandbox) as T;
  facades.add(proxy);
  const capture = Object.freeze({ sandbox: proxy, close() { closed = true; } });
  captures.add(capture);
  return capture;
}

/** Idempotently stop only a handle returned by instrumentE2bSandbox. */
export function uninstrumentE2bSandbox(capture: { sandbox: unknown; close(): void }): void {
  if (captures.has(capture)) capture.close();
}
