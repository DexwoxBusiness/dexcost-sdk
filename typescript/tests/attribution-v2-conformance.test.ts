import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { describe, expect, it } from "vitest";

import { createCostEvent } from "../src/core/models.js";
import { toAttributionEventV2 } from "../src/attribution/convert.js";
import { ATTRIBUTION_V2_CONTRACT_VERSION } from "../src/attribution/types.js";
import { validateAttributionEventV2 } from "../src/attribution/validate.js";

interface CorpusCase {
  name: string;
  event: unknown;
  expected_error_path?: string;
}

const corpusPath = fileURLToPath(
  new URL("../../fixtures/attribution_v2/conformance.json", import.meta.url),
);
const corpus = JSON.parse(readFileSync(corpusPath, "utf8")) as {
  contract_version: string;
  valid: CorpusCase[];
  invalid: CorpusCase[];
};

describe("attribution v2 shared conformance corpus", () => {
  it("pins the same contract version as the control plane", () => {
    expect(corpus.contract_version).toBe(ATTRIBUTION_V2_CONTRACT_VERSION);
  });

  for (const testCase of corpus.valid) {
    it(`accepts ${testCase.name}`, () => {
      expect(validateAttributionEventV2(testCase.event)).toEqual({ success: true, issues: [] });
    });
  }

  for (const testCase of corpus.invalid) {
    it(`rejects ${testCase.name}`, () => {
      const result = validateAttributionEventV2(testCase.event);
      expect(result.success).toBe(false);
      expect(result.issues.map((issue) => issue.path)).toContain(testCase.expected_error_path);
    });
  }

  it.each([
    "2026-02-29T10:00:00Z",
    "2026-04-31T10:00:00Z",
  ])("rejects impossible calendar date %s", (occurredAt) => {
    const event = structuredClone(corpus.valid[0].event) as Record<string, unknown>;
    event.occurred_at = occurredAt;
    const result = validateAttributionEventV2(event);
    expect(result.success).toBe(false);
    expect(result.issues.map((issue) => issue.path)).toContain("occurred_at");
  });
});

describe("v1 capture to attribution v2 conversion", () => {
  const base = {
    eventId: randomUUID(),
    taskId: randomUUID(),
    occurredAt: new Date("2026-07-16T10:00:00.123Z"),
  };

  it("keeps Anthropic cache buckets disjoint and carries versioned catalog evidence", () => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      eventType: "llm_call",
      provider: "anthropic",
      model: "claude-sonnet-4-5",
      inputTokens: 100,
      cachedTokens: 1000,
      outputTokens: 50,
      costUsd: "0.00135",
      costConfidence: "exact",
      pricingSource: "service_catalog",
      pricingVersion: "llm:2026-07-16",
      details: { cache_creation_input_tokens: 25 },
    }));

    expect(converted?.usage).toEqual([
      { metric: "input_tokens", quantity: "100", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "1000", unit: "Tokens" },
      { metric: "cache_write_input_tokens", quantity: "25", unit: "Tokens" },
      { metric: "output_tokens", quantity: "50", unit: "Tokens" },
    ]);
    expect(converted?.cost_evidence).toMatchObject({ source: "sdk_catalog", confidence: "computed" });
  });

  it("subtracts OpenAI cached tokens from inclusive input tokens", () => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      eventType: "llm_call",
      provider: "openai",
      inputTokens: 1200,
      cachedTokens: 1000,
      outputTokens: 50,
    }));
    expect(converted?.usage.slice(0, 2)).toEqual([
      { metric: "input_tokens", quantity: "200", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "1000", unit: "Tokens" },
    ]);
  });

  it("promotes compute quantities and closes their usage period", () => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      eventType: "compute_cost",
      costConfidence: "computed",
      details: {
        billing_model: "lambda",
        duration_ms: 2500,
        memory_bytes_limit: 2 * 1024 ** 3,
        vcpu_seconds_used: 2.5,
        invocation_count: 1,
        region: "us-east-1",
      },
    }));
    expect(converted?.component).toBe("compute");
    expect(converted?.usage).toContainEqual({ metric: "memory_gib_seconds", quantity: "5", unit: "GiB-Seconds" });
    expect(converted?.usage_period?.end_at).toBe(converted?.occurred_at);
  });

  it.each([
    {
      eventType: "compute_cost" as const,
      pricingSource: "compute_catalog:aws:lambda:us-east-1:x86_64" as const,
      pricingVersion: "compute:1.0.0",
      details: { billing_model: "lambda", duration_ms: 1000, invocation_count: 1 },
    },
    {
      eventType: "gpu_cost" as const,
      pricingSource: "gpu_catalog:runpod:per_gpu_second_active:a100" as const,
      pricingVersion: "gpu:1.0.0",
      details: { billing_model: "per_gpu_second_active", gpu_seconds_used: 1, duration_ms: 1000 },
    },
    {
      eventType: "network" as const,
      pricingSource: "egress_catalog:aws:us-east-1" as const,
      pricingVersion: "egress:1.0.0",
      details: { request_bytes: 1000 },
    },
  ])("preserves $pricingSource as versioned SDK catalog evidence", (spec) => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      ...spec,
      costUsd: "0.09",
      costConfidence: "exact",
    }));
    expect(converted?.cost_evidence).toEqual({
      amount: "0.09",
      currency: "USD",
      source: "sdk_catalog",
      confidence: "computed",
      pricing_version: spec.pricingVersion,
    });
  });

  it.each([
    {
      eventType: "compute_cost" as const,
      details: { billing_model: "ec2", vcpu_seconds_used: 2.5 },
      metric: "vcpu_seconds",
    },
    {
      eventType: "gpu_cost" as const,
      details: { billing_model: "per_gpu_second_active", gpu_seconds_used: 2.5 },
      metric: "gpu_seconds",
    },
  ])("keeps $eventType with active-time usage when wall duration is unavailable", (spec) => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      eventType: spec.eventType,
      details: spec.details,
    }));
    expect(converted).not.toBeNull();
    expect(converted?.usage.map((line) => line.metric)).toContain(spec.metric);
    expect(converted?.usage_period).toEqual({
      start_at: converted?.occurred_at,
      end_at: converted?.occurred_at,
    });
  });

  it("keeps network directions separate", () => {
    const converted = toAttributionEventV2(createCostEvent({
      ...base,
      eventType: "network",
      serviceName: "api.example.com",
      details: { request_bytes: 123, response_bytes: 456 },
    }));
    expect(converted?.usage).toEqual([
      { metric: "bytes_out", quantity: "123", unit: "Bytes" },
      { metric: "bytes_in", quantity: "456", unit: "Bytes" },
    ]);
  });

  it("drops overlapping retry markers and observability-only GPU signals", () => {
    expect(toAttributionEventV2(createCostEvent({ ...base, eventType: "retry_marker" }))).toBeNull();
    expect(toAttributionEventV2(createCostEvent({ ...base, eventType: "gpu_utilization_signal" }))).toBeNull();
  });

  it("uses a stable observed_at and never transmits arbitrary details", () => {
    const internal = createCostEvent({
      ...base,
      eventType: "external_cost",
      serviceName: "tavily",
      details: { secret: "must-not-leave-process" },
    });
    const first = toAttributionEventV2(internal);
    const second = toAttributionEventV2(internal);
    expect(first).toEqual(second);
    expect(first?.observed_at).toBe(first?.occurred_at);
    expect(first).not.toHaveProperty("details");
  });
});
