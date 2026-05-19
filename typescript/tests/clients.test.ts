/**
 * Tests for TrackedOpenAI and TrackedAnthropic wrapper classes.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { CostTracker } from "../src/core/tracker.js";
import { setContext, clearContext } from "../src/core/context.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-clients-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// TrackedOpenAI
// ---------------------------------------------------------------------------

describe("TrackedOpenAI", () => {
  it("records llm_call event on chat.completions.create inside tracked task", async () => {
    const { TrackedOpenAI } = await import("../src/clients.js");

    const mockResponse = {
      id: "chatcmpl-abc",
      model: "gpt-4o",
      choices: [],
      usage: {
        prompt_tokens: 800,
        completion_tokens: 150,
        prompt_tokens_details: { cached_tokens: 50 },
      },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      chat: {
        completions: {
          create: mockCreate,
        },
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db"), autoInstrument: [] });

    await tracker.track({ taskType: "chat" }, async (_trackedTask) => {
      const client = new TrackedOpenAI({ client: mockClient, tracker });
      const response = await client.chat.completions.create({
        model: "gpt-4o",
        messages: [{ role: "user", content: "Hello" }],
      });

      // Should pass through the original response
      expect((response as typeof mockResponse).model).toBe("gpt-4o");
      // Mock should have been called
      expect(mockCreate).toHaveBeenCalledOnce();
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("openai");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(800);
    expect(events[0].outputTokens).toBe(150);
    expect(events[0].cachedTokens).toBe(50);
    expect(events[0].costConfidence).toBe("computed");

    tracker.close();
  });

  it("records into an auto-task when no task and no context set", async () => {
    const { TrackedOpenAI } = await import("../src/clients.js");

    const mockResponse = {
      id: "chatcmpl-abc",
      model: "gpt-4o",
      choices: [],
      usage: { prompt_tokens: 100, completion_tokens: 50 },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      chat: {
        completions: {
          create: mockCreate,
        },
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db"), autoInstrument: [] });

    // Call outside any tracked task context
    const client = new TrackedOpenAI({ client: mockClient, tracker });
    const response = await client.chat.completions.create({
      model: "gpt-4o",
      messages: [],
    });

    // Response should still pass through
    expect((response as typeof mockResponse).model).toBe("gpt-4o");

    // The cost is recorded into an auto-task so it is never silently lost.
    const events = tracker.buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);
    expect(
      tracker.buffer.getAllTasks().some((t) => t.taskType === "openai.chat"),
    ).toBe(true);

    tracker.close();
  });

  it("creates auto-task with setContext when no explicit task", async () => {
    const { TrackedOpenAI } = await import("../src/clients.js");

    const mockResponse = {
      id: "chatcmpl-abc",
      model: "gpt-4o",
      choices: [],
      usage: { prompt_tokens: 200, completion_tokens: 80 },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      chat: {
        completions: {
          create: mockCreate,
        },
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test2.db"), autoInstrument: [] });

    setContext({ customerId: "auto-client-openai-test" });

    const client = new TrackedOpenAI({ client: mockClient, tracker });
    const response = await client.chat.completions.create({
      model: "gpt-4o",
      messages: [],
    });

    expect((response as typeof mockResponse).model).toBe("gpt-4o");

    const events = tracker.buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = tracker.buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-client-openai-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("openai.chat");

    clearContext();
    tracker.close();
  });
});

// ---------------------------------------------------------------------------
// TrackedAnthropic
// ---------------------------------------------------------------------------

describe("TrackedAnthropic", () => {
  it("records llm_call event on messages.create inside tracked task", async () => {
    const { TrackedAnthropic } = await import("../src/clients.js");

    const mockResponse = {
      id: "msg_abc123",
      type: "message",
      model: "claude-3-5-sonnet-20241022",
      role: "assistant",
      content: [{ type: "text", text: "Hello!" }],
      usage: {
        input_tokens: 500,
        output_tokens: 100,
        cache_creation_input_tokens: 200,
        cache_read_input_tokens: 50,
      },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      messages: {
        create: mockCreate,
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db"), autoInstrument: [] });

    await tracker.track({ taskType: "chat" }, async (_trackedTask) => {
      const client = new TrackedAnthropic({ client: mockClient, tracker });
      const response = await client.messages.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{ role: "user", content: "Hello" }],
      });

      // Should pass through the original response
      expect((response as typeof mockResponse).model).toBe("claude-3-5-sonnet-20241022");
      // Mock should have been called
      expect(mockCreate).toHaveBeenCalledOnce();
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("anthropic");
    expect(events[0].model).toBe("claude-3-5-sonnet-20241022");
    expect(events[0].inputTokens).toBe(500);
    expect(events[0].outputTokens).toBe(100);
    // cachedTokens = cache_read_input_tokens (50)
    expect(events[0].cachedTokens).toBe(50);
    expect(events[0].costConfidence).toBe("computed");

    tracker.close();
  });

  it("records into an auto-task when no task and no context set", async () => {
    const { TrackedAnthropic } = await import("../src/clients.js");

    const mockResponse = {
      id: "msg_abc",
      type: "message",
      model: "claude-3-5-sonnet-20241022",
      role: "assistant",
      content: [],
      usage: { input_tokens: 100, output_tokens: 50 },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      messages: {
        create: mockCreate,
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db"), autoInstrument: [] });

    // Call outside any tracked task context
    const client = new TrackedAnthropic({ client: mockClient, tracker });
    const response = await client.messages.create({
      model: "claude-3-5-sonnet-20241022",
      max_tokens: 1024,
      messages: [],
    });

    // Response should still pass through
    expect((response as typeof mockResponse).model).toBe("claude-3-5-sonnet-20241022");

    // The cost is recorded into an auto-task so it is never silently lost.
    const events = tracker.buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);
    expect(
      tracker.buffer.getAllTasks().some((t) => t.taskType === "anthropic.messages"),
    ).toBe(true);

    tracker.close();
  });

  it("creates auto-task with setContext when no explicit task", async () => {
    const { TrackedAnthropic } = await import("../src/clients.js");

    const mockResponse = {
      id: "msg_xyz",
      type: "message",
      model: "claude-3-5-sonnet-20241022",
      role: "assistant",
      content: [],
      usage: { input_tokens: 300, output_tokens: 60, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    };
    const mockCreate = vi.fn().mockResolvedValue(mockResponse);
    const mockClient = {
      messages: {
        create: mockCreate,
      },
    };

    const tracker = new CostTracker({ dbPath: join(tmpDir, "test2.db"), autoInstrument: [] });

    setContext({ customerId: "auto-client-anthropic-test" });

    const client = new TrackedAnthropic({ client: mockClient, tracker });
    const response = await client.messages.create({
      model: "claude-3-5-sonnet-20241022",
      max_tokens: 1024,
      messages: [],
    });

    expect((response as typeof mockResponse).model).toBe("claude-3-5-sonnet-20241022");

    const events = tracker.buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = tracker.buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-client-anthropic-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("anthropic.messages");

    clearContext();
    tracker.close();
  });
});
