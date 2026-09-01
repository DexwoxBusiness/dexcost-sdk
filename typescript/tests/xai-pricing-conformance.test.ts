import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  canonicalXaiModel,
  tokenMeasurement,
  xaiPricingLane,
} from "../src/instruments/provider-extract.js";

interface LaneCase {
  id: string;
  usage: Record<string, unknown> | null;
  total_input_tokens: number;
  service_tier?: string;
  expected: string | null;
}

interface TickCase {
  ticks: unknown;
  expected_usd: string | null;
}

const fixture = JSON.parse(readFileSync(
  new URL("../../fixtures/xai_pricing_conformance.json", import.meta.url),
  "utf8",
)) as {
  model_cases: Record<string, string>;
  lane_cases: LaneCase[];
  tick_cases: TickCase[];
};

describe("shared xAI pricing conformance", () => {
  for (const [reported, expected] of Object.entries(fixture.model_cases)) {
    it(`canonicalizes ${reported}`, () => {
      expect(canonicalXaiModel(reported)).toBe(expected);
    });
  }

  for (const testCase of fixture.lane_cases) {
    it(testCase.id, () => {
      const response = testCase.usage === null
        ? { service_tier: testCase.service_tier }
        : { usage: testCase.usage, service_tier: testCase.service_tier };
      expect(xaiPricingLane(response, testCase.total_input_tokens))
        .toBe(testCase.expected ?? undefined);
    });
  }

  for (const [index, testCase] of fixture.tick_cases.entries()) {
    it(`converts USD ticks case ${index + 1}`, () => {
      const amount = tokenMeasurement({
        usage: {
          prompt_tokens: 1,
          completion_tokens: 1,
          cost_in_usd_ticks: testCase.ticks,
        },
      }, "grok-4.6", "xai").providerCostUsd;
      expect(amount?.toString()).toBe(testCase.expected_usd ?? undefined);
    });
  }

  it("keeps alias and cost handling provider-scoped", () => {
    expect(tokenMeasurement({
      usage: { prompt_tokens: 1, completion_tokens: 1, cost_in_usd_ticks: 10_000_000_000 },
    }, "grok-4.6", "openai").providerCostUsd).toBeUndefined();
  });
});
