// Cross-SDK parity test for the TypeScript SDK.
//
// Consumes the canonical fixture corpus at <repo>/fixtures/ produced by
// python/tests/test_cross_sdk_parity.py. Asserts the TS SDK round-trips
// events / tasks and produces pricing output that matches the Python-
// canonical expected outputs.
//
// This suite is intentionally RED on initial commit. Each failing
// sub-test pins an audit finding scheduled for Sprint 1+:
//   - P1  occurred_at timestamp format drift
//   - P2  PricingSource enum spelling drift
//   - P4  network event 4xx-below-threshold emission
//   - B11 streaming-body corruption (downstream)
//   - LLM cost map drift (TS missing ~708 keys vs Python catalog)
//   - URL scrubber absent in TS (Theme A, Sprint 1)
//
// As each finding lands the corresponding sub-test must flip green.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname, basename, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  eventFromDict,
  eventToDict,
  taskFromDict,
  taskToDict,
  PricingEngine,
} from "../src/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_ROOT = resolve(__dirname, "..", "..", "fixtures");

function readJson(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(path, "utf-8"));
}

function stripUnderscoredKeys(d: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(d)) {
    if (k.startsWith("_")) continue;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      out[k] = stripUnderscoredKeys(v as Record<string, unknown>);
    } else {
      out[k] = v;
    }
  }
  return out;
}

function expectedPathFor(rel: string, kind: string): string {
  // Map fixture-input rel paths to their expected-output counterparts.
  //   events/foo.json                 -> canonical_serialization/foo.json
  //   events/edge_cases/foo.json      -> canonical_serialization/edge_cases/foo.json
  //   tasks/foo.json                  -> canonical_serialization/tasks/foo.json
  //   pricing_inputs/X/foo.json       -> pricing/X/foo.json
  const base = basename(rel);
  if (rel.startsWith("pricing_inputs/")) {
    return join(FIXTURES_ROOT, "expected_outputs", kind, rel.slice("pricing_inputs/".length));
  }
  if (rel.includes("edge_cases/")) {
    return join(FIXTURES_ROOT, "expected_outputs", kind, "edge_cases", base);
  }
  if (rel.startsWith("tasks/")) {
    return join(FIXTURES_ROOT, "expected_outputs", kind, "tasks", base);
  }
  return join(FIXTURES_ROOT, "expected_outputs", kind, base);
}

const EVENT_FIXTURES = [
  "events/llm_call.v1.json",
  "events/external_cost.v1.json",
  "events/compute_cost_lambda.v1.json",
  "events/compute_cost_ec2_share.v1.json",
  "events/compute_cost_k8s_pod.v1.json",
  "events/network.v1.json",
  "events/network_4xx_below_threshold.v1.json",
  "events/gpu_cost.v1.json",
  "events/gpu_utilization_signal.v1.json",
  "events/retry_marker.v1.json",
  "events/edge_cases/tiny_decimal.v1.json",
];

const TASK_FIXTURES = ["tasks/task_minimal.v1.json", "tasks/task_with_network_gpu.v1.json"];

describe("cross-SDK event canonical serialization", () => {
  for (const rel of EVENT_FIXTURES) {
    it(rel, () => {
      const input = stripUnderscoredKeys(readJson(join(FIXTURES_ROOT, rel)));
      const expected = readJson(expectedPathFor(rel, "canonical_serialization"));
      const evt = eventFromDict(input);
      // Round-trip through JSON to normalize Date/number/Decimal types into
      // the same shape as the expected file.
      const actual = JSON.parse(JSON.stringify(eventToDict(evt)));
      expect(actual).toEqual(expected);
    });
  }
});

describe("cross-SDK task canonical serialization", () => {
  for (const rel of TASK_FIXTURES) {
    it(rel, () => {
      const input = stripUnderscoredKeys(readJson(join(FIXTURES_ROOT, rel)));
      const expected = readJson(expectedPathFor(rel, "canonical_serialization"));
      const task = taskFromDict(input);
      const actual = JSON.parse(JSON.stringify(taskToDict(task)));
      expect(actual).toEqual(expected);
    });
  }
});

describe("cross-SDK LLM pricing parity", () => {
  const engine = new PricingEngine();
  const LLM_FIXTURES = [
    "pricing_inputs/llm/gpt4o_500_in_200_out.json",
    "pricing_inputs/llm/claude_sonnet_streaming_2000_in_1500_out.json",
  ];
  for (const rel of LLM_FIXTURES) {
    it(rel, () => {
      const input = stripUnderscoredKeys(readJson(join(FIXTURES_ROOT, rel))) as Record<
        string,
        number | string
      >;
      const expected = readJson(expectedPathFor(rel, "pricing"));
      const result = engine.getCost(
        input.model as string,
        input.input_tokens as number,
        input.output_tokens as number,
        (input.cached_tokens as number) ?? 0,
        0,
      );
      // Decimal equality (not string equality) — TS uses number, so trailing
      // zeros / repr differ from Python's Decimal stringification. Drift in
      // the numeric value itself (cost-map differences) is the real signal.
      expect(Number(result.costUsd)).toBeCloseTo(Number(expected.cost_usd), 8);
      expect(result.pricingSource).toBe(expected.pricing_source);
    });
  }
});

describe.skip("cross-SDK URL scrubber parity", () => {
  // Sprint 1 / Theme A: TS SDK has no URL scrubber. Pin the gap.
  // expected_outputs/security/url_with_*.v1.json defines the canonical algorithm.
  it("TODO(sprint-1, theme-a): implement scrubUrl() in TS SDK security module", () => {});
});

describe("cross-SDK tiny-decimal accumulation invariant (B3)", () => {
  it("1.23E-8 summed 10000 times equals 0.0001230000 exactly", () => {
    const expected = readJson(
      join(FIXTURES_ROOT, "expected_outputs", "pricing", "decimal_accumulation_invariant.json"),
    );
    const per = Number(expected.per_event_cost_usd);
    const iters = Number(expected.iterations);
    const wantTotal = Number(expected.total_cost_usd);

    let total = 0;
    for (let i = 0; i < iters; i++) {
      total += per;
    }
    // RED today: native JS number arithmetic drifts. B3 fix in Sprint 2
    // (decimal.js everywhere) flips this green.
    expect(total).toBe(wantTotal);
  });
});
