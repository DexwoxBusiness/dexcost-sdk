/**
 * Tests for Express/Connect middleware.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { CostTracker } from "../src/core/tracker.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});
import { createExpressMiddleware } from "../src/middleware/express.js";

/** Create a minimal mock Express request. */
function mockReq(
  method = "POST",
  path = "/api/chat",
  user?: { orgId: string },
): Record<string, unknown> {
  return { method, path, originalUrl: path, user };
}

/** Create a minimal mock Express response with an event emitter. */
function mockRes(): Record<string, unknown> & { _emit: (event: string) => void } {
  const listeners: Record<string, Array<() => void>> = {};
  return {
    statusCode: 200,
    on(event: string, fn: () => void) {
      (listeners[event] ??= []).push(fn);
    },
    _emit(event: string) {
      for (const fn of listeners[event] ?? []) fn();
    },
  };
}

describe("Express middleware", () => {
  it("creates a task per request and calls next", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker);
    const req = mockReq("POST", "/api/chat");
    const res = mockRes();

    let nextCalled = false;
    mw(req, res, () => {
      nextCalled = true;
    });

    expect(nextCalled).toBe(true);

    // Simulate request completing
    res.statusCode = 200;
    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  });

  it("attaches dexcostTask to the request object", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker);
    const req = mockReq("GET", "/api/data");
    const res = mockRes();

    mw(req, res, () => {
      // Inside next(), the task should be attached
    });

    expect(req["dexcostTask"]).toBeDefined();

    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  });

  it("extracts customerId from configured dot-path", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker, {
      customerIdFrom: "user.orgId",
    });
    const req = mockReq("GET", "/api/data", { orgId: "acme" });
    const res = mockRes();

    mw(req, res, () => {});
    expect((req["dexcostTask"] as { task: { customerId?: string } }).task.customerId).toBe("acme");

    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  });

  it("extracts projectId from configured dot-path", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker, {
      projectIdFrom: "headers.x-project-id",
    });
    const req = { method: "GET", path: "/api/data", originalUrl: "/api/data", headers: { "x-project-id": "proj-42" } };
    const res = mockRes();

    mw(req, res, () => {});
    expect((req["dexcostTask"] as { task: { projectId?: string } }).task.projectId).toBe("proj-42");

    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  }, 10_000);

  it("skips requests when skip returns true", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker, {
      skip: (req) => (req as Record<string, unknown>)["path"] === "/health",
    });
    const req = mockReq("GET", "/health");
    const res = mockRes();

    let nextCalled = false;
    mw(req, res, () => {
      nextCalled = true;
    });

    expect(nextCalled).toBe(true);
    // dexcostTask should NOT be set when skipped
    expect(req["dexcostTask"]).toBeUndefined();

    tracker.close();
  });

  it("uses custom taskType function", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker, {
      taskType: (req) =>
        `api:${(req as Record<string, unknown>)["method"]}:${(req as Record<string, unknown>)["path"]}`,
    });
    const req = mockReq("POST", "/api/chat");
    const res = mockRes();

    mw(req, res, () => {});
    expect((req["dexcostTask"] as { task: { taskType: string } }).task.taskType)
      .toBe("api:POST:/api/chat");

    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  });

  it("defaults to a canonical task type and keeps route details in metadata", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker);
    const req = mockReq("DELETE", "/api/items/123");
    const res = mockRes();

    mw(req, res, () => {});
    const task = (req["dexcostTask"] as {
      task: { taskType: string; metadata: Record<string, unknown> };
    }).task;
    expect(task.taskType).toBe("http.request");
    expect(task.metadata).toMatchObject({ httpMethod: "DELETE", httpRoute: "/api/items/123" });

    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    tracker.close();
  });

  it("ends task as failed when status >= 400", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const mw = createExpressMiddleware(tracker);
    const req = mockReq("POST", "/api/chat");
    const res = mockRes();

    mw(req, res, () => {});

    // Simulate a 500 error response
    res.statusCode = 500;
    res._emit("finish");
    await new Promise((r) => setTimeout(r, 50));

    // The task should have been ended as "failed"
    const task = (req["dexcostTask"] as { task: { status: string } }).task;
    expect(task.status).toBe("failed");

    tracker.close();
  });
});
