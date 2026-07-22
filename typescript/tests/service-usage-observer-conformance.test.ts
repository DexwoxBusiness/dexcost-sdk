import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";

import { ServiceUsageObservers } from "../src/pricing/service-usage-observers.js";
import { _providerObservationEventId } from "../src/adapters/http.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "../../fixtures/service_usage_observation_conformance.json"), "utf8")) as {
  cases: Array<{ name: string; url: string; headers: Record<string, string>; request?: unknown; response: unknown; expected: Array<Record<string, string>> }>;
};

describe("shared service usage observer conformance", () => {
  const observers = new ServiceUsageObservers();

  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const observed = observers.observe(
        testCase.url,
        new Headers(testCase.headers),
        testCase.response,
        testCase.request,
      );
      expect(observed).toHaveLength(testCase.expected.length);
      for (let index = 0; index < testCase.expected.length; index++) {
        expect(observed[index]).toMatchObject({
          serviceKey: testCase.expected[index].service_key,
          providerName: testCase.expected[index].provider_name,
          providerService: testCase.expected[index].provider_service,
          component: testCase.expected[index].component,
          metric: testCase.expected[index].metric,
          quantity: testCase.expected[index].quantity,
        });
        expect(observed[index].resourceType).toBe(testCase.expected[index].resource_type);
        expect(observed[index].resourceId).toBe(testCase.expected[index].resource_id);
        expect(observed[index].providerRecordId).toBe(testCase.expected[index].provider_record_id);
      }
    });
  }

  it("keeps the packaged observer manifest equal to the canonical manifest", () => {
    const canonical = JSON.parse(readFileSync(join(here, "../../fixtures/service_usage_observers.json"), "utf8"));
    const packaged = JSON.parse(readFileSync(join(here, "../src/data/service_usage_observers.json"), "utf8"));
    expect(packaged).toEqual(canonical);
  });

  it("keeps provider observation IDs stable across SDK languages", () => {
    expect(_providerObservationEventId(
      {
        serviceKey: "assemblyai_transcription",
        providerName: "assemblyai",
        providerService: "speech_to_text_pre_recorded",
        component: "speech_to_text",
        metric: "audio_seconds",
        quantity: "1",
        providerRecordId: "aa-123",
        manifestVersion: "1.4.0",
      },
    )).toBe("2dc521b3-742a-5f61-9942-c4a59e6935f6");
  });
});
