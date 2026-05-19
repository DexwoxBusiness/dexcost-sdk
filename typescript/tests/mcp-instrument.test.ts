import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import {
  instrumentMcp,
  uninstrumentMcp,
  _setClientClass,
  _resetClientClass,
} from "../src/instruments/mcp.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-mcp-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMcpResult(overrides: Record<string, unknown> = {}) {
  return {
    content: [{ type: "text", text: "search result" }],
    isError: false,
    ...overrides,
  };
}

class FakeClient {
  async callTool(
    params: { name: string; arguments?: Record<string, unknown> },
    ..._rest: unknown[]
  ): Promise<unknown> {
    return makeMcpResult();
  }
}

describe("MCP instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setClientClass(FakeClient);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentMcp();
    _resetClientClass();
    clearContext();
  });

  it("records external_cost event inside tracked task", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const result = await fake.callTool({ name: "tavily_search", arguments: { q: "test" } });
      expect((result as Record<string, unknown>).isError).toBe(false);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("external_cost");
    expect(events[0].serviceName).toBe("mcp:tavily_search");
    expect(events[0].costUsd).toBeGreaterThanOrEqual(0);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("includes mcp_tool and mcp_server in details", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "brave_web_search", arguments: { q: "hello" } });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    const details = events[0].details;
    expect(details.mcp_tool).toBe("brave_web_search");
    expect(details.mcp_server).toBeDefined();
    expect(details.latency_ms).toBeGreaterThanOrEqual(0);
    expect(details.is_error).toBe(false);
  });

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();

    const result = await fake.callTool({ name: "tavily_search" });
    expect((result as Record<string, unknown>).isError).toBe(false);
    // Costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(buffer.getAllTasks().some((t) => t.taskType === "mcp.tool_call")).toBe(
      true,
    );
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-cust" });
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();

    await fake.callTool({ name: "exa_search" });
    clearContext();

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("external_cost");
    expect(events[0].serviceName).toBe("mcp:exa_search");

    const tasks = buffer.getAllTasks();
    expect(tasks.length).toBeGreaterThanOrEqual(1);
    const autoTask = tasks.find((t) => t.taskType === "mcp.tool_call");
    expect(autoTask).toBeDefined();
    expect(autoTask!.customerId).toBe("auto-cust");
  });

  it("records error when tool call throws", async () => {
    class FailingClient {
      async callTool(): Promise<unknown> {
        throw new Error("MCP server unreachable");
      }
    }
    _setClientClass(FailingClient);
    await instrumentMcp(pricing, buffer);
    const fake = new FailingClient();
    const task = createTask({ taskId: randomUUID(), taskType: "error-test" });

    await runWithTask(task, async () => {
      await expect(fake.callTool({ name: "broken_tool" })).rejects.toThrow("MCP server unreachable");
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].details.is_error).toBe(true);
    expect(events[0].serviceName).toBe("mcp:broken_tool");
  });

  it("detects isError flag on MCP result", async () => {
    class ErrorResultClient {
      async callTool(): Promise<unknown> {
        return makeMcpResult({ isError: true });
      }
    }
    _setClientClass(ErrorResultClient);
    await instrumentMcp(pricing, buffer);
    const fake = new ErrorResultClient();
    const task = createTask({ taskId: randomUUID(), taskType: "error-result" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "bad_tool" });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].details.is_error).toBe(true);
  });

  it("unknown tool gets zero cost with unknown confidence", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "unknown" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "my_totally_custom_tool" });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd).toBe(0);
    expect(events[0].costConfidence).toBe("unknown");
  });

  it("known tool gets estimated confidence from service catalog mapping", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "mapped" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "tavily_search" });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    // tavily_search is in MCP_TOOL_MAP -> gets "estimated" confidence
    expect(events[0].costConfidence).toBe("estimated");
  });

  it("aggregates external cost on task", async () => {
    await instrumentMcp(pricing, buffer);
    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "aggregate" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "tavily_search" });
      await fake.callTool({ name: "firecrawl_scrape" });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(2);

    const tasks = buffer.getAllTasks();
    const updatedTask = tasks.find((t) => t.taskId === task.taskId);
    expect(updatedTask).toBeDefined();
  });

  it("does not double-patch on second call", async () => {
    await instrumentMcp(pricing, buffer);
    await instrumentMcp(pricing, buffer); // second call should be no-op

    const fake = new FakeClient();
    const task = createTask({ taskId: randomUUID(), taskType: "double" });

    await runWithTask(task, async () => {
      await fake.callTool({ name: "test_tool" });
    });

    // Should still only record one event, not two
    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
  });

  it("restores original after uninstrument", async () => {
    const originalCreate = FakeClient.prototype.callTool;
    await instrumentMcp(pricing, buffer);
    expect(FakeClient.prototype.callTool).not.toBe(originalCreate);

    uninstrumentMcp();
    expect(FakeClient.prototype.callTool).toBe(originalCreate);
  });
});
