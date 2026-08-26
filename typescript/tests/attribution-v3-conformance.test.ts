import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { createCostEvent } from "../src/core/models.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { ATTRIBUTION_V3_CONTRACT_VERSION } from "../src/attribution/v3-types.js";
import { validateAttributionObservationV3 } from "../src/attribution/v3-validate.js";

interface ValidCase {
  id: string;
  event: Record<string, unknown>;
}

interface InvalidCase {
  id: string;
  expected_error_path: string;
  event?: Record<string, unknown>;
  mutate_from?: string;
  set?: Record<string, unknown>;
  delete?: string[];
  append_usage?: unknown;
  append_dimension?: unknown;
}

const corpusPath = fileURLToPath(
  new URL("../../fixtures/attribution_v3/conformance.json", import.meta.url),
);
const canonicalSchemaPath = fileURLToPath(
  new URL("../../fixtures/attribution_v3/schemas.json", import.meta.url),
);
const packagedSchemaPath = fileURLToPath(
  new URL("../src/attribution/attribution-v3-schema.json", import.meta.url),
);
const localGpuPath = fileURLToPath(
  new URL("../../fixtures/attribution_v3/local_gpu_usage.json", import.meta.url),
);
const corpus = JSON.parse(readFileSync(corpusPath, "utf8")) as {
  observation_contract_version: string;
  valid_observations: ValidCase[];
  invalid_observations: InvalidCase[];
};
const validById = new Map(corpus.valid_observations.map((entry) => [entry.id, entry.event]));
const localGpu = JSON.parse(readFileSync(localGpuPath, "utf8")) as {
  details: Record<string, unknown>;
  expected: Record<string, string | boolean>;
};

function parentAndKey(target: Record<string, unknown>, path: string): {
  parent: Record<string, unknown> | unknown[];
  key: string;
} {
  const parts = path.split(".");
  let parent: Record<string, unknown> | unknown[] = target;
  for (const part of parts.slice(0, -1)) {
    parent = (parent as Record<string, Record<string, unknown> | unknown[]>)[part];
  }
  return { parent, key: parts.at(-1)! };
}

function materialize(testCase: InvalidCase): Record<string, unknown> {
  if (testCase.event !== undefined) return structuredClone(testCase.event);
  const base = testCase.mutate_from === undefined ? undefined : validById.get(testCase.mutate_from);
  if (base === undefined) throw new Error(`Unknown corpus base ${String(testCase.mutate_from)}`);
  const event = structuredClone(base);
  for (const [path, value] of Object.entries(testCase.set ?? {})) {
    const { parent, key } = parentAndKey(event, path);
    (parent as Record<string, unknown>)[key] = structuredClone(value);
  }
  for (const path of testCase.delete ?? []) {
    const { parent, key } = parentAndKey(event, path);
    delete (parent as Record<string, unknown>)[key];
  }
  if (testCase.append_usage !== undefined) {
    (event.usage as unknown[]).push(structuredClone(testCase.append_usage));
  }
  if (testCase.append_dimension !== undefined) {
    const firstUsage = (event.usage as Array<{ dimensions: unknown[] }>)[0];
    firstUsage.dimensions.push(structuredClone(testCase.append_dimension));
  }
  return event;
}

describe("attribution v3 shared conformance corpus", () => {
  it("pins the control-plane contract version", () => {
    expect(corpus.observation_contract_version).toBe(ATTRIBUTION_V3_CONTRACT_VERSION);
  });
  it("packages the byte-identical authoritative schema", () => {
    expect(readFileSync(packagedSchemaPath, "utf8")).toBe(
      readFileSync(canonicalSchemaPath, "utf8"),
    );
  });

  for (const testCase of corpus.valid_observations) {
    it(`accepts ${testCase.id}`, () => {
      expect(validateAttributionObservationV3(testCase.event)).toEqual({
        success: true,
        issues: [],
      });
    });
  }

  for (const testCase of corpus.invalid_observations) {
    it(`rejects ${testCase.id} at its promised path`, () => {
      const result = validateAttributionObservationV3(materialize(testCase));
      expect(result.success).toBe(false);
      expect(result.issues.map((issue) => issue.path)).toContain(testCase.expected_error_path);
    });
  }
});

describe("durable v1 capture to attribution v3 conversion", () => {
  const base = {
    eventId: "11111111-1111-4111-8111-111111111111",
    taskId: "22222222-2222-4222-8222-222222222222",
    occurredAt: new Date("2026-08-11T10:00:00.123Z"),
  };

  it("emits full v3 observations with stable operation and usage identities", () => {
    const event = createCostEvent({
      ...base,
      eventType: "llm_call",
      provider: "anthropic",
      model: "claude-sonnet-4-5",
      inputTokens: 100,
      cachedTokens: 1_000,
      outputTokens: 50,
      costUsd: "0.00135",
      costConfidence: "exact",
      pricingSource: "service_catalog",
      pricingVersion: "llm:2026-08-11",
      details: { cache_creation_input_tokens: 25 },
    });
    const first = toAttributionObservationV3(event);
    const second = toAttributionObservationV3(event);

    expect(first).toEqual(second);
    expect(first).toMatchObject({
      schema_version: "3",
      event_id: base.eventId,
      task_id: base.taskId,
      usage_snapshot: "full",
      operation: {
        id: base.eventId,
        status: "succeeded",
        attempt: { id: base.eventId, number: 1 },
      },
      cost_evidence: {
        source: "sdk_catalog",
        confidence: "computed",
        pricing_version: "llm:2026-08-11",
      },
    });
    expect(first?.usage).toHaveLength(4);
    expect(first?.usage.every((line) => line.dimensions.length === 0)).toBe(true);
    expect(new Set(first?.usage.map((line) => line.line_id)).size).toBe(4);
    expect(first).not.toHaveProperty("details");
    expect(validateAttributionObservationV3(first).success).toBe(true);
  });

  it("does not invent a request when successful compute usage is absent", () => {
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventType: "compute_cost",
      details: {},
    }));
    expect(converted).toBeNull();
  });

  it("places retry linkage on the operation attempt", () => {
    const retryOf = randomUUID();
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventId: randomUUID(),
      eventType: "retry_marker",
      isRetry: true,
      retryReason: "rate_limit",
      retryOf,
      costUsd: "0.02",
      costConfidence: "exact",
      details: {
        attribution_operation_id: retryOf,
        attribution_attempt_number: 2,
      },
    }));
    expect(converted).toMatchObject({
      operation: {
        id: retryOf,
        status: "failed",
        attempt: { number: 2, retry_of: retryOf },
      },
      resource: { type: "other", id: "rate_limit" },
    });
    expect(converted).not.toHaveProperty("retry_of");
  });

  it("retains an unknown explicit meter as visibly unpriced", () => {
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventType: "external_cost",
      serviceName: "future-provider",
      details: {
        attribution_component: "telephony",
        attribution_usage_metric: "provider_new_meter",
        attribution_usage_unit: "Widgets",
        attribution_usage_quantity: "7.5",
        attribution_dimensions: [
          { key: "priority", value: { type: "string", value: "fast" } },
        ],
      },
    }));
    expect(converted?.component).toBe("telephony");
    expect(converted?.usage).toEqual([expect.objectContaining({
      metric: "provider_new_meter",
      unit: "Widgets",
      quantity: "7.5",
      dimensions: [{ key: "priority", value: { type: "string", value: "fast" } }],
    })]);
    expect(converted).not.toHaveProperty("cost_evidence");
  });

  it("preserves provider-native multiline usage on known event types", () => {
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventType: "llm_call",
      provider: "openrouter",
      model: "openrouter/anthropic/claude-sonnet-4",
      inputTokens: 999,
      outputTokens: 999,
      details: {
        attribution_usage_duration_seconds: "2.5",
        attribution_usage_lines: [
          { metric: "input_tokens", quantity: "10", unit: "Tokens" },
          { metric: "cache_read_input_tokens", quantity: "20", unit: "Tokens" },
          { metric: "output_tokens", quantity: "5", unit: "Tokens" },
        ],
      },
    }));
    expect(converted?.component).toBe("llm");
    expect(converted?.usage.map(({ metric, quantity, unit }) => ({ metric, quantity, unit })))
      .toEqual([
        { metric: "input_tokens", quantity: "10", unit: "Tokens" },
        { metric: "cache_read_input_tokens", quantity: "20", unit: "Tokens" },
        { metric: "output_tokens", quantity: "5", unit: "Tokens" },
      ]);
    expect(converted?.usage_period).toEqual({
      start_at: "2026-08-11T09:59:57.623000Z",
      end_at: "2026-08-11T10:00:00.123000Z",
    });
  });

  it("rejects empty, oversized, duplicate, and malformed multiline usage", () => {
    const rows = Array.from({ length: 33 }, (_, index) => ({
      metric: `meter_${index}`, quantity: "1", unit: "Units",
    }));
    for (const usage of [
      [], rows,
      [{ metric: "input_tokens", quantity: "1", unit: "Tokens" },
        { metric: "input_tokens", quantity: "2", unit: "Tokens" }],
      [{ metric: "Input Tokens", quantity: "1", unit: "Tokens" }],
    ]) {
      expect(toAttributionObservationV3(createCostEvent({
        ...base, eventType: "llm_call", details: { attribution_usage_lines: usage },
      }))).toBeNull();
    }
  });

  it("retains GPU utilization as non-monetary extensible meters", () => {
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventType: "gpu_utilization_signal",
      details: {
        gpu_index: 0,
        gpu_sku: "h100",
        sm_util_pct: 42.5,
        vram_used_peak_bytes: 1024,
        task_duration_ms: 60_000,
      },
    }));
    expect(converted).toMatchObject({
      component: "gpu",
      operation: { status: "unknown" },
      usage_period: {
        start_at: "2026-08-11T09:59:00.123000Z",
        end_at: "2026-08-11T10:00:00.123000Z",
      },
    });
    expect(converted?.usage.map((line) => line.metric)).toEqual([
      "gpu.sm_utilization_percent",
      "gpu.vram_peak_bytes",
    ]);
    expect(converted).not.toHaveProperty("cost_evidence");
  });

  it("matches the shared local-GPU usage-only contract", () => {
    const converted = toAttributionObservationV3(createCostEvent({
      ...base,
      eventType: "gpu_cost",
      costUsd: 0,
      details: localGpu.details,
    }));
    expect(converted).toMatchObject({
      component: localGpu.expected.component,
      provider: {
        name: localGpu.expected.provider_name,
        service: localGpu.expected.provider_service,
      },
      resource: {
        type: localGpu.expected.resource_type,
        id: localGpu.expected.resource_id,
      },
      usage: [{
        metric: localGpu.expected.usage_metric,
        unit: localGpu.expected.usage_unit,
        quantity: localGpu.expected.usage_quantity,
        dimensions: [],
      }],
    });
    expect(converted).not.toHaveProperty("cost_evidence");
    expect(validateAttributionObservationV3(converted!)).toMatchObject({ success: true });
  });
});
