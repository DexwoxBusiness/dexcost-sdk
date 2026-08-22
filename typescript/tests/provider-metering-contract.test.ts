import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { Decimal } from "../src/core/models.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  ProviderOperationSession,
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
});
