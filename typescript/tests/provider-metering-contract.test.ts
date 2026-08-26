import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { Decimal } from "../src/core/models.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  ProviderOperationSession,
  wrapProviderStream,
  validateOperationMeasurement,
} from "../src/instruments/provider-metering.js";

describe("provider measurement runtime contract", () => {
  it("accepts exact non-negative quantities and canonical dimensions", () => {
    expect(() => validateOperationMeasurement({
      usageLines: [
        { metric: "input_tokens", quantity: 2, unit: "Tokens" },
        { metric: "audio_seconds", quantity: new Decimal("0.125"), unit: "Seconds" },
        { metric: "request_count", quantity: "1", unit: "Requests" },
      ],
      providerCostUsd: "0.00000125",
      billingDimensions: [["gateway", "openrouter"]],
      inputTokens: 2,
    })).not.toThrow();
  });

  it("rejects binary fractional numbers, booleans, negatives, and non-finite quantities", () => {
    for (const quantity of [0.1, true, -1, "NaN", "Infinity"] as unknown[]) {
      expect(() => validateOperationMeasurement({
        usageLines: [{ metric: "input_tokens", quantity: quantity as never, unit: "Tokens" }],
      })).toThrow();
    }
  });

  it("rejects invalid and duplicate positive usage identities", () => {
    expect(() => validateOperationMeasurement({
      usageLines: [{ metric: "Input Tokens", quantity: 1, unit: "Tokens" }],
    })).toThrow(/invalid provider usage metric/);
    expect(() => validateOperationMeasurement({
      usageLines: [{ metric: "input_tokens", quantity: 1, unit: "token value" }],
    })).toThrow(/invalid provider usage unit/);
    expect(() => validateOperationMeasurement({
      usageLines: [
        { metric: "input_tokens", quantity: 1, unit: "Tokens" },
        { metric: "input_tokens", quantity: 2, unit: "Tokens" },
      ],
    })).toThrow(/duplicate provider usage line/);
  });

  it("rejects duplicate, invalid, empty, oversized, and excessive dimensions", () => {
    expect(() => validateOperationMeasurement({
      billingDimensions: [["gateway", "a"], ["gateway", "b"]],
    })).toThrow(/duplicate provider billing dimension/);
    expect(() => validateOperationMeasurement({ billingDimensions: [["Gateway Name", "a"]] }))
      .toThrow(/invalid provider billing dimension/);
    expect(() => validateOperationMeasurement({ billingDimensions: [["gateway", ""]] }))
      .toThrow(/1-256/);
    expect(() => validateOperationMeasurement({
      billingDimensions: Array.from({ length: 25 }, (_, index) => [`key_${index}`, "value"] as const),
    })).toThrow(/cannot exceed 24/);
  });

  it("rejects invalid task token rollups", () => {
    for (const inputTokens of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => validateOperationMeasurement({ inputTokens })).toThrow(/inputTokens/);
    }
  });
});

describe("provider measurement fail-open boundary", () => {
  let directory: string;
  let buffer: EventBuffer;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-provider-contract-"));
    buffer = new EventBuffer(join(directory, "events.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("does not persist a malformed adapter event or throw into provider code", () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.invalid",
      provider: "test",
      service: "test",
      operation: "test.invalid",
      component: "external",
    });
    const result = session.finish({
      usageLines: [
        { metric: "request_count", quantity: 1, unit: "Requests" },
        { metric: "request_count", quantity: 2, unit: "Requests" },
      ],
    });
    expect(result).toBeUndefined();
    expect(buffer.getAllEvents()).toEqual([]);
    expect(buffer.getAllTasks()[0]?.status).toBe("failed");
  });

  it("retains partial usage when an async provider stream fails", async () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.stream", provider: "test", service: "chat",
      operation: "test.chat", component: "llm", model: "gpt-5", eventType: "llm_call",
    });
    let inputTokens = 0;
    const raw = {
      async *[Symbol.asyncIterator](): AsyncGenerator<{ input: number }> {
        yield { input: 7 };
        throw new Error("stream failed");
      },
    };
    const stream = wrapProviderStream(
      session.invoke(() => raw), session,
      (item) => { inputTokens = (item as { input: number }).input; },
      () => ({
        usageLines: [{ metric: "input_tokens", quantity: inputTokens, unit: "Tokens" }],
        pricingUsage: { input_tokens: inputTokens }, inputTokens,
      }),
    );
    await expect(async () => {
      for await (const _item of stream) { /* consume until failure */ }
    }).rejects.toThrow("stream failed");
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0]).toMatchObject({ inputTokens: 7 });
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("failed");
  });

  it("does not replace async provider values when terminal extraction fails", async () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.stream", provider: "test", service: "chat",
      operation: "test.chat", component: "llm", model: "gpt-5", eventType: "llm_call",
    });
    const raw = {
      async *[Symbol.asyncIterator](): AsyncGenerator<number> {
        yield 2;
        yield 3;
      },
    };
    const extractionFailure = (): never => { throw new Error("unsupported provider shape"); };
    const stream = wrapProviderStream(
      session.invoke(() => raw), session,
      extractionFailure,
      extractionFailure,
      extractionFailure,
    );
    const values: number[] = [];
    for await (const item of stream) values.push(item);

    expect(values).toEqual([2, 3]);
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("unknown");
    expect(buffer.getAllEvents()[0].details.attribution_usage_lines).toEqual([
      { metric: "request_count", quantity: "1", unit: "Requests" },
    ]);
  });

  it("does not replace sync provider values when terminal extraction fails", () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.stream", provider: "test", service: "chat",
      operation: "test.chat", component: "llm", model: "gpt-5", eventType: "llm_call",
    });
    const extractionFailure = (): never => { throw new Error("unsupported provider shape"); };
    const stream = wrapProviderStream(
      session.invoke(() => [4, 5]), session,
      extractionFailure,
      extractionFailure,
      extractionFailure,
    );

    expect([...stream]).toEqual([4, 5]);
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("unknown");
  });

  it("records cancellation when early close usage extraction fails", () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.stream", provider: "test", service: "chat",
      operation: "test.chat", component: "llm", model: "gpt-5", eventType: "llm_call",
    });
    const extractionFailure = (): never => { throw new Error("unsupported provider shape"); };
    const stream = wrapProviderStream(
      session.invoke(() => [6, 7]), session,
      () => undefined,
      extractionFailure,
    );
    const iterator = stream[Symbol.iterator]();

    expect(iterator.next()).toEqual({ value: 6, done: false });
    expect(iterator.return?.()).toEqual({ value: undefined, done: true });
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("cancelled");
  });

  it("degrades a malformed terminal measurement to safe request evidence", () => {
    const session = new ProviderOperationSession(new PricingEngine(), buffer, {
      taskType: "provider.stream", provider: "test", service: "chat",
      operation: "test.chat", component: "llm", model: "gpt-5", eventType: "llm_call",
    });
    const stream = wrapProviderStream(
      session.invoke(() => [8]), session,
      () => undefined,
      () => ({
        usageLines: [
          { metric: "request_count", quantity: 1, unit: "Requests" },
          { metric: "request_count", quantity: 2, unit: "Requests" },
        ],
      }),
    );

    expect([...stream]).toEqual([8]);
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("succeeded");
    expect(buffer.getAllEvents()[0].details.attribution_usage_lines).toEqual([
      { metric: "request_count", quantity: "1", unit: "Requests" },
    ]);
  });

  it("records one outer provider event when a gateway invokes an instrumented provider", () => {
    const pricing = new PricingEngine();
    const outer = new ProviderOperationSession(pricing, buffer, {
      taskType: "litellm.completion", provider: "litellm", service: "litellm",
      operation: "litellm.completion", component: "llm", model: "openai/gpt-5",
      eventType: "llm_call",
    });
    outer.invoke(() => {
      const inner = new ProviderOperationSession(pricing, buffer, {
        taskType: "openai.chat", provider: "openai", service: "chat",
        operation: "openai.chat.create", component: "llm", model: "gpt-5",
        eventType: "llm_call",
      });
      inner.invoke(() => undefined);
      inner.finish({ usageLines: [{ metric: "input_tokens", quantity: 3, unit: "Tokens" }] });
    });
    outer.finish({ usageLines: [{ metric: "input_tokens", quantity: 3, unit: "Tokens" }] });
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].provider).toBe("litellm");
  });
});
