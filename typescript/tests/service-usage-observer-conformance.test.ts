import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";

import { ServiceUsageObservers } from "../src/pricing/service-usage-observers.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "../../fixtures/service_usage_observation_conformance.json"), "utf8")) as {
  cases: Array<{ name: string; url: string; headers: Record<string, string>; response: unknown; expected: Record<string, string> | null }>;
};

describe("shared service usage observer conformance", () => {
  const observers = new ServiceUsageObservers();

  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const observed = observers.observe(
        testCase.url,
        new Headers(testCase.headers),
        testCase.response,
      );
      if (testCase.expected === null) {
        expect(observed).toBeNull();
        return;
      }
      expect(observed).toMatchObject({
        serviceKey: testCase.expected.service_key,
        providerName: testCase.expected.provider_name,
        providerService: testCase.expected.provider_service,
        component: testCase.expected.component,
        metric: testCase.expected.metric,
        quantity: testCase.expected.quantity,
      });
      expect(observed?.resourceId).toBe(testCase.expected.resource_id);
      expect(observed?.providerRecordId).toBe(testCase.expected.provider_record_id);
    });
  }

  it("keeps the packaged observer manifest equal to the canonical manifest", () => {
    const canonical = JSON.parse(readFileSync(join(here, "../../fixtures/service_usage_observers.json"), "utf8"));
    const packaged = JSON.parse(readFileSync(join(here, "../src/data/service_usage_observers.json"), "utf8"));
    expect(packaged).toEqual(canonical);
  });
});
