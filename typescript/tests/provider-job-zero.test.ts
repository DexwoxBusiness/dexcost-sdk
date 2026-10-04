import { afterEach, expect, it } from "vitest";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Decimal } from "../src/core/models.js";
import { ProviderJobRevision, providerJobFromDict } from "../src/core/provider-jobs.js";
import { providerJobMeasurementFields } from "../src/instruments/provider-metering.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { EventBuffer } from "../src/transport/buffer.js";

let buffer: EventBuffer;
let directory: string;
afterEach(() => { buffer?.close(); if (directory) rmSync(directory, { recursive: true, force: true }); });
const base = () => ({ taskId: randomUUID(), provider: "perplexity", service: "agent", providerRecordId: "zero-job",
  operation: "perplexity.responses.create", component: "llm", eventType: "llm_call" as const,
  resourceType: "model" as const, resourceId: "perplexity/fixture", status: "succeeded" as const,
  usage: [{ metric: "request_count", quantity: new Decimal(1), unit: "Requests" }],
});

it("preserves explicit provider zero through measurement, storage and v3 wire; missing is distinct", () => {
  const pricing = new PricingEngine();
  const usageLines = [{ metric: "request_count", quantity: 1, unit: "Requests" }];
  const exact = providerJobMeasurementFields(pricing, "fixture", { usageLines, pricingUsage: {}, providerCostUsd: "0" });
  const missing = providerJobMeasurementFields(pricing, "fixture", { usageLines, pricingUsage: {} });
  expect(exact.costAmount?.toString()).toBe("0"); expect(missing.costAmount).toBeUndefined();
  const job = new ProviderJobRevision({ ...base(), ...exact });
  directory = mkdtempSync(join(tmpdir(), "dexcost-job-zero-")); buffer = new EventBuffer(join(directory, "events.db"));
  buffer.insertProviderJobRevision(job);
  const restored = providerJobFromDict(buffer.getProviderJob("perplexity", "agent", "zero-job")!);
  expect(restored.toAttributionObservation().cost_evidence).toEqual({ amount: "0", currency: "USD", source: "provider_reported", confidence: "exact" });
  expect(restored.toDict().cost_amount).toBe("0");
  expect(() => buffer.insertProviderJobRevision(job)).not.toThrow();
  expect(buffer.getProviderJob("perplexity", "agent", "zero-job")?.revision).toBe(1);
});

it.each([
  { costSource: "provider_reported", costConfidence: "estimated" },
  { costSource: "sdk_catalog", costConfidence: "computed", pricingVersion: "fixture" },
  { costSource: "provider_reported", costConfidence: "exact", status: "failed" },
  { costSource: "provider_reported", costConfidence: "exact", status: "cancelled" },
])("does not accept synthesized/non-final zero %j", fields => {
  expect(() => new ProviderJobRevision({ ...base(), ...fields, costAmount: new Decimal(0) } as any)).toThrow();
});
