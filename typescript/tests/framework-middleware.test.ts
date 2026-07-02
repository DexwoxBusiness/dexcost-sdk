/**
 * Tests for the Fastify plugin and Hono middleware, plus the Express
 * middleware's tracker-optional overload. All adapters are dependency-free
 * (structural typing), so the tests drive them with faithful fakes.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { CostTracker } from "../src/core/tracker.js";
import { getCurrentTask } from "../src/core/context.js";
import { dexcostFastifyPlugin } from "../src/middleware/fastify.js";
import { createHonoMiddleware } from "../src/middleware/hono.js";
import { createExpressMiddleware } from "../src/middleware/express.js";
import { _resetMiddlewareWarningsForTests } from "../src/middleware/shared.js";

let tmpDir: string;
let tracker: CostTracker;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-fwmw-test-"));
  tracker = new CostTracker({
    dbPath: join(tmpDir, "test.db"),
    autoInstrument: [],
    trackHttp: false,
  });
  _resetMiddlewareWarningsForTests();
});

afterEach(() => {
  tracker.close();
  vi.restoreAllMocks();
  rmSync(tmpDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Fastify
// ---------------------------------------------------------------------------

/** Minimal fake of a Fastify instance capturing registered hooks. */
function makeFakeFastify() {
  const hooks = new Map<string, Array<(...args: unknown[]) => void>>();
  return {
    addHook(name: string, fn: (...args: unknown[]) => void) {
      const list = hooks.get(name) ?? [];
      list.push(fn);
      hooks.set(name, list);
    },
    hooks,
  };
}

describe("dexcostFastifyPlugin", () => {
  it("registers hooks, tracks a request, and finalizes on response", async () => {
    const app = makeFakeFastify();
    await new Promise<void>((resolve, reject) =>
      dexcostFastifyPlugin(app, { tracker, customerIdFrom: "headers.x-customer-id" }, (err) =>
        err ? reject(err) : resolve(),
      ),
    );
    expect([...app.hooks.keys()].sort()).toEqual(["onError", "onRequest", "onResponse"]);

    const request: Record<string, unknown> = {
      method: "POST",
      url: "/review?token=secret",
      headers: { "x-customer-id": "acme" },
    };
    const reply = { statusCode: 200 };

    // onRequest: the continuation must run INSIDE the task's ALS scope.
    let taskIdInsideHandler = "";
    await new Promise<void>((resolve) =>
      app.hooks.get("onRequest")![0](request, reply, () => {
        taskIdInsideHandler = getCurrentTask()!.taskId;
        resolve();
      }),
    );
    expect(taskIdInsideHandler).toBeTruthy();
    expect(request.dexcostTask).toBeDefined();

    await new Promise<void>((resolve) =>
      app.hooks.get("onResponse")![0](request, reply, () => resolve()),
    );

    const task = tracker.buffer.getAllTasks().find((t) => t.taskId === taskIdInsideHandler)!;
    expect(task.status).toBe("success");
    expect(task.customerId).toBe("acme");
    expect(task.taskType).toBe("POST /review?token=REDACTED");
  });

  it("marks the task failed on error and on >=400 responses", async () => {
    const app = makeFakeFastify();
    await new Promise<void>((resolve) => dexcostFastifyPlugin(app, { tracker }, () => resolve()));

    // Error hook path
    const req1: Record<string, unknown> = { method: "GET", url: "/a" };
    await new Promise<void>((r) => app.hooks.get("onRequest")![0](req1, {}, () => r()));
    await new Promise<void>((r) =>
      app.hooks.get("onError")![0](req1, {}, new Error("boom"), () => r()),
    );
    // onResponse after onError must not double-end (status no longer pending)
    await new Promise<void>((r) =>
      app.hooks.get("onResponse")![0](req1, { statusCode: 500 }, () => r()),
    );

    // 4xx status path
    const req2: Record<string, unknown> = { method: "GET", url: "/b" };
    await new Promise<void>((r) => app.hooks.get("onRequest")![0](req2, {}, () => r()));
    await new Promise<void>((r) =>
      app.hooks.get("onResponse")![0](req2, { statusCode: 404 }, () => r()),
    );

    const tasks = tracker.buffer.getAllTasks();
    expect(tasks.find((t) => t.taskType === "GET /a")!.status).toBe("failed");
    expect(tasks.find((t) => t.taskType === "GET /b")!.status).toBe("failed");
  });

  it("skip predicate bypasses tracking entirely", async () => {
    const app = makeFakeFastify();
    await new Promise<void>((resolve) =>
      dexcostFastifyPlugin(
        app,
        { tracker, skip: (req: any) => req.url === "/health" },
        () => resolve(),
      ),
    );
    const request: Record<string, unknown> = { method: "GET", url: "/health" };
    await new Promise<void>((r) => app.hooks.get("onRequest")![0](request, {}, () => r()));
    expect(request.dexcostTask).toBeUndefined();
    expect(tracker.buffer.getAllTasks()).toHaveLength(0);
  });

  it("declares fastify-plugin encapsulation opt-out", () => {
    expect((dexcostFastifyPlugin as any)[Symbol.for("skip-override")]).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Hono
// ---------------------------------------------------------------------------

function makeFakeHonoContext(method = "POST", path = "/review", status = 200) {
  const vars = new Map<string, unknown>();
  return {
    req: {
      method,
      path,
      header: (name: string) => (name === "x-customer-id" ? "acme" : undefined),
    },
    res: { status },
    set: (k: string, v: unknown) => vars.set(k, v),
    get: (k: string) => vars.get(k),
  };
}

describe("createHonoMiddleware", () => {
  it("tracks the request inside the task scope and finalizes from res.status", async () => {
    const mw = createHonoMiddleware({
      tracker,
      customerId: (c) => c.req.header("x-customer-id"),
    });
    const c = makeFakeHonoContext();

    let taskIdInsideHandler = "";
    await mw(c, async () => {
      taskIdInsideHandler = getCurrentTask()!.taskId;
    });

    const task = tracker.buffer.getAllTasks().find((t) => t.taskId === taskIdInsideHandler)!;
    expect(task.status).toBe("success");
    expect(task.customerId).toBe("acme");
    expect(task.taskType).toBe("POST /review");
    expect((c.get("dexcostTask") as any).task.taskId).toBe(taskIdInsideHandler);
  });

  it("marks failed on handler throw and propagates the error", async () => {
    const mw = createHonoMiddleware({ tracker });
    const c = makeFakeHonoContext();
    const boom = new Error("handler exploded");

    await expect(
      mw(c, async () => {
        throw boom;
      }),
    ).rejects.toBe(boom);

    expect(tracker.buffer.getAllTasks()[0].status).toBe("failed");
  });

  it("marks failed on >=400 response status", async () => {
    const mw = createHonoMiddleware({ tracker });
    const c = makeFakeHonoContext("GET", "/x", 503);
    await mw(c, async () => {});
    expect(tracker.buffer.getAllTasks()[0].status).toBe("failed");
  });

  it("skip predicate bypasses tracking; handler still runs", async () => {
    const mw = createHonoMiddleware({ tracker, skip: (c) => c.req.path === "/health" });
    const c = makeFakeHonoContext("GET", "/health");
    let ran = false;
    await mw(c, async () => {
      ran = true;
    });
    expect(ran).toBe(true);
    expect(tracker.buffer.getAllTasks()).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Express overloads
// ---------------------------------------------------------------------------

describe("createExpressMiddleware overloads", () => {
  it("legacy (tracker, options) signature still works", async () => {
    const mw = createExpressMiddleware(tracker, { customerIdFrom: "user.orgId" });
    const req: Record<string, unknown> = {
      method: "GET",
      originalUrl: "/things",
      user: { orgId: "acme" },
    };
    const listeners = new Map<string, () => void>();
    const res = {
      statusCode: 200,
      on: (event: string, cb: () => void) => listeners.set(event, cb),
    };

    await new Promise<void>((resolve) => {
      mw(req, res, () => resolve());
    });
    listeners.get("finish")!();
    await new Promise((r) => setImmediate(r));

    const task = tracker.buffer.getAllTasks()[0];
    expect(task.customerId).toBe("acme");
    expect(task.status).toBe("success");
  });

  it("options-only signature warns once and passes through when init() was never called", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    // Singleton is not initialized in this test file — the middleware must
    // not throw and must let requests through.
    const mw = createExpressMiddleware({ customerIdFrom: "user.orgId" });
    const req = { method: "GET", originalUrl: "/a" };
    const res = { statusCode: 200, on: () => {} };

    let nextCalls = 0;
    mw(req, res, () => nextCalls++);
    mw(req, res, () => nextCalls++);

    expect(nextCalls).toBe(2);
    const dexcostWarnings = warn.mock.calls.filter((c) =>
      String(c[0]).includes("express middleware"),
    );
    expect(dexcostWarnings).toHaveLength(1);
  });
});
