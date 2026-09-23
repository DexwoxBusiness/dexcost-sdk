import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { groqPricingLane } from "../src/instruments/provider-extract.js";

interface LaneCase {
  id: string;
  service_tier: string | null;
  executed_tools: unknown;
  expected: string | null;
}

const fixture = JSON.parse(readFileSync(
  new URL("../../fixtures/groq_pricing_conformance.json", import.meta.url),
  "utf8",
)) as { lane_cases: LaneCase[] };

describe("shared Groq pricing conformance", () => {
  for (const testCase of fixture.lane_cases) {
    it(testCase.id, () => {
      expect(groqPricingLane({
        service_tier: testCase.service_tier,
        choices: [{ message: { executed_tools: testCase.executed_tools } }],
      })).toBe(testCase.expected ?? undefined);
    });
  }

  it("keeps all Groq money on the authoritative server", () => {
    const costMap = JSON.parse(readFileSync(
      new URL("../src/pricing/cost_map.json", import.meta.url),
      "utf8",
    )) as Record<string, Record<string, unknown>>;
    const entries = Object.entries(costMap).filter(([model]) => model.startsWith("groq/"));
    expect(entries.length).toBeGreaterThan(0);
    for (const [model, metadata] of entries) {
      const moneyFields = Object.keys(metadata).filter((key) => key.includes("cost") || key.includes("price"));
      expect(moneyFields, model).toEqual([]);
    }
  });
});
