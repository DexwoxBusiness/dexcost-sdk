import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  canonicalMistralModel,
  mistralPricingLane,
} from "../src/instruments/provider-extract.js";

interface LaneCase {
  id: string;
  service_tier: string | null;
  expected: string | null;
}

interface SurfaceCase {
  id: string;
  surface: string;
  expected: string | null;
}

const fixture = JSON.parse(readFileSync(
  new URL("../../fixtures/mistral_pricing_conformance.json", import.meta.url),
  "utf8",
)) as {
  model_cases: Record<string, string>;
  lane_cases: LaneCase[];
  surface_cases: SurfaceCase[];
};

describe("shared Mistral pricing conformance", () => {
  for (const [reported, expected] of Object.entries(fixture.model_cases)) {
    it(`canonicalizes ${reported}`, () => {
      expect(canonicalMistralModel(reported)).toBe(expected);
    });
  }

  for (const testCase of fixture.lane_cases) {
    it(testCase.id, () => {
      expect(mistralPricingLane({
        usage: { service_tier: testCase.service_tier },
      }, "chat_completions")).toBe(testCase.expected ?? undefined);
    });
  }

  for (const testCase of fixture.surface_cases) {
    it(testCase.id, () => {
      expect(mistralPricingLane({
        usage: { service_tier: "standard" },
      }, testCase.surface)).toBe(testCase.expected ?? undefined);
    });
  }

  it("keeps direct Mistral money on the authoritative server", () => {
    const costMap = JSON.parse(readFileSync(
      new URL("../src/pricing/cost_map.json", import.meta.url),
      "utf8",
    )) as Record<string, Record<string, unknown>>;
    for (const model of Object.values(fixture.model_cases)) {
      expect(costMap[model], model).toBeUndefined();
    }
  });
});
