/**
 * Tests for the P1 integration surface: wrapJobHandler, the NestJS
 * interceptor, createDexcostFetch, and the wrap* client entry points.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { of, throwError } from "rxjs";
import { CostTracker } from "../src/core/tracker.js";
import { getCurrentTask, runWithTask, clearContext } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { randomUUID } from "node:crypto";
import { wrapJobHandler } from "../src/adapters/worker-wrap.js";
import { DexcostInterceptor } from "../src/middleware/nestjs.js";
import { _resetMiddlewareWarningsForTests } from "../src/middleware/shared.js";
import { wrapOpenAI, wrapAnthropic } from "../src/clients.js";
import {
  createDexcostFetch,
  trackHttp,
  untrackHttp,
  clearRecordedEvents,
  resetServiceCatalog,
} from "../src/adapters/http.js";

let tmpDir: string;
let tracker: CostTracker;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-p1-test-"));
  tracker = new CostTracker({
    dbPath: join(tmpDir, "test.db"),
    autoInstrument: [],
    trackHttp: false,
  });
  _resetMiddlewareWarningsForTests();
  clearContext();
});

afterEach(() => {
  tracker.close();
  untrackHttp();
  clearRecordedEvents();
  resetServiceCatalog();
  clearContext();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  rmSync(tmpDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// wrapJobHandler
// ---------------------------------------------------------------------------

describe("wrapJobHandler", () => {
  it("runs each job in its own attributed task with ALS scope", async () => {
    const seenTaskIds: string[] = [];
    const handler = wrapJobHandler(
      async (job: { orgId: string; prNumber: number }) => {
        seenTaskIds.push(getCurrentTask()!.taskId);
        return `reviewed-${job.prNumber}`;
      },
      {
        tracker,
        taskType: "code_review",
        customerId: (job) => job.orgId,
        metadata: (job) => ({ pr_number: job.prNumber }),
      },
    );

    const r1 = await handler({ orgId: "acme", prNumber: 1 });
    const r2 = await handler({ orgId: "globex", prNumber: 2 });
    expect(r1).toBe("reviewed-1");
    expect(r2).toBe("reviewed-2");
    expect(seenTaskIds[0]).not.toBe(seenTaskIds[1]);

    const tasks = tracker.buffer.getAllTasks();
    expect(tasks).toHaveLength(2);
    expect(tasks.every((t) => t.taskType === "code_review")).toBe(true);
    expect(tasks.every((t) => t.status === "success")).toBe(true);
    expect(tasks.map((t) => t.customerId).sort()).toEqual(["acme", "globex"]);
  });

  it("marks the task failed and rethrows so queue retry semantics survive", async () => {
    const boom = new Error("job exploded");
    const handler = wrapJobHandler(
      async () => {
        throw boom;
      },
      { tracker, taskType: "flaky_job" },
    );
    await expect(handler()).rejects.toBe(boom);
    expect(tracker.buffer.getAllTasks()[0].status).toBe("failed");
  });

  it("a throwing extractor degrades attribution without killing the job", async () => {
    const handler = wrapJobHandler(async () => "done", {
      tracker,
      customerId: () => {
        throw new Error("bad payload");
      },
    });
    await expect(handler()).resolves.toBe("done");
    const task = tracker.buffer.getAllTasks()[0];
    expect(task.status).toBe("success");
    expect(task.customerId).toBeUndefined();
  });

  it("passes through (warn once) when dexcost is not initialized", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const handler = wrapJobHandler(async (x: number) => x * 2);
    expect(await handler(21)).toBe(42);
    expect(await handler(2)).toBe(4);
    expect(warn.mock.calls.filter((c) => String(c[0]).includes("worker"))).toHaveLength(1);
  });

  it("defaults taskType to the handler's function name", async () => {
    async function reviewPullRequest(): Promise<void> {}
    await wrapJobHandler(reviewPullRequest, { tracker })();
    expect(tracker.buffer.getAllTasks()[0].taskType).toBe("reviewPullRequest");
  });
});

// ---------------------------------------------------------------------------
// DexcostInterceptor (NestJS)
// ---------------------------------------------------------------------------

function makeHttpContext(request: Record<string, unknown>, response: Record<string, unknown>) {
  return {
    switchToHttp: () => ({
      getRequest: () => request,
      getResponse: () => response,
    }),
  };
}

function subscribeToCompletion(obs: any): Promise<unknown[]> {
  return new Promise((resolve, reject) => {
    const values: unknown[] = [];
    obs.subscribe({
      next: (v: unknown) => values.push(v),
      error: reject,
      complete: () => resolve(values),
    });
  });
}

describe("DexcostInterceptor (NestJS)", () => {
  it("tracks the request, runs the handler chain in the task scope, finalizes from status", async () => {
    const interceptor = new DexcostInterceptor({
      tracker,
      customerIdFrom: "headers.x-customer-id",
    });
    const request: Record<string, unknown> = {
      method: "POST",
      originalUrl: "/reviews",
      headers: { "x-customer-id": "acme" },
    };
    const context = makeHttpContext(request, { statusCode: 201 });

    let taskIdInsideHandler = "";
    const next = {
      handle: () =>
        of(null).pipe(
          // The controller runs on subscription — capture the ALS task there.
          (source) =>
            new (of(null).constructor as any)((subscriber: any) => {
              taskIdInsideHandler = getCurrentTask()?.taskId ?? "";
              return source.subscribe(subscriber);
            }),
        ),
    };

    const values = await subscribeToCompletion(interceptor.intercept(context, next));
    expect(values).toEqual([null]);
    expect(taskIdInsideHandler).toBeTruthy();

    const task = tracker.buffer.getAllTasks().find((t) => t.taskId === taskIdInsideHandler)!;
    expect(task.status).toBe("success");
    expect(task.customerId).toBe("acme");
    expect(task.taskType).toBe("POST /reviews");
    expect((request.dexcostTask as any).task.taskId).toBe(taskIdInsideHandler);
  });

  it("marks failed on handler error and propagates it to exception filters", async () => {
    const interceptor = new DexcostInterceptor({ tracker });
    const context = makeHttpContext({ method: "GET", url: "/x" }, { statusCode: 200 });
    const boom = new Error("controller exploded");
    const next = { handle: () => throwError(() => boom) };

    await expect(subscribeToCompletion(interceptor.intercept(context, next))).rejects.toBe(boom);
    expect(tracker.buffer.getAllTasks()[0].status).toBe("failed");
  });

  it("marks failed on >=400 response status", async () => {
    const interceptor = new DexcostInterceptor({ tracker });
    const context = makeHttpContext({ method: "GET", url: "/x" }, { statusCode: 404 });
    await subscribeToCompletion(interceptor.intercept(context, { handle: () => of("nope") }));
    expect(tracker.buffer.getAllTasks()[0].status).toBe("failed");
  });

  it("passes through non-HTTP contexts and skipped requests untracked", async () => {
    const interceptor = new DexcostInterceptor({
      tracker,
      skip: (req: any) => req.url === "/health",
    });
    // Non-HTTP context (no switchToHttp)
    const rpcResult = await subscribeToCompletion(
      interceptor.intercept({}, { handle: () => of(1) }),
    );
    expect(rpcResult).toEqual([1]);
    // Skipped request
    const skipResult = await subscribeToCompletion(
      interceptor.intercept(makeHttpContext({ method: "GET", url: "/health" }, {}), {
        handle: () => of(2),
      }),
    );
    expect(skipResult).toEqual([2]);
    expect(tracker.buffer.getAllTasks()).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// createDexcostFetch
// ---------------------------------------------------------------------------

describe("createDexcostFetch", () => {
  it("captures LLM calls without any global patch", async () => {
    const base = vi.fn(async () =>
      new Response(
        JSON.stringify({ model: "kimi-k2", usage: { input_tokens: 42, output_tokens: 7 } }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    ) as unknown as typeof globalThis.fetch;

    const tracked = createDexcostFetch({ tracker, fetch: base });
    expect(tracked).not.toBe(base);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    await runWithTask(task, async () => {
      const res = await tracked("https://api.kimi.com/anthropic/v1/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0].inputTokens).toBe(42);
  });

  it("refuses to double-wrap an already-instrumented fetch", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    trackHttp(tracker.buffer, tracker.pricing);
    // globalThis.fetch is now dexcost-instrumented; wrapping again would
    // double-count every call.
    const result = createDexcostFetch({ tracker });
    expect(result).toBe(globalThis.fetch);
  });

  it("returns the base fetch unwrapped (loudly) when nothing is wired", async () => {
    // Fresh module state: untrack + no tracker passed and singleton absent.
    untrackHttp();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const base = (async () => new Response("{}")) as unknown as typeof globalThis.fetch;
    const result = createDexcostFetch({ fetch: base });
    expect(result).toBe(base);
    expect(warn.mock.calls.some((c) => String(c[0]).includes("createDexcostFetch"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// wrap* client entry points
// ---------------------------------------------------------------------------

describe("wrapOpenAI / wrapAnthropic", () => {
  it("wrap a client instance and record llm_call events", async () => {
    const fakeOpenAI = {
      chat: {
        completions: {
          create: async () => ({
            model: "gpt-4o",
            usage: { prompt_tokens: 100, completion_tokens: 20 },
            choices: [],
          }),
        },
      },
    };
    const wrapped = wrapOpenAI(fakeOpenAI, { tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    await runWithTask(task, async () => {
      await wrapped.chat.completions.create({ model: "gpt-4o", messages: [] });
    });
    const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0].inputTokens).toBe(100);
  });

  it("wrapAnthropic exposes the messages surface", () => {
    const fakeAnthropic = { messages: { create: async () => ({}) } };
    const wrapped = wrapAnthropic(fakeAnthropic, { tracker });
    expect(typeof wrapped.messages.create).toBe("function");
  });
});
