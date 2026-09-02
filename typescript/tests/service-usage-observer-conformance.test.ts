import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";

import { ServiceUsageObservers } from "../src/pricing/service-usage-observers.js";
import { _providerObservationEventId } from "../src/adapters/http.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "../../fixtures/service_usage_observation_conformance.json"), "utf8")) as {
  cases: Array<{ name: string; url: string; method?: string; status_code?: number; headers: Record<string, string>; request_headers?: string[] | Record<string, string>; request?: unknown; response: unknown; expected: Array<Record<string, string>> }>;
};

describe("shared service usage observer conformance", () => {
  const observers = new ServiceUsageObservers();

  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const statusCode = testCase.status_code ?? 200;
      const observed = statusCode >= 200 && statusCode < 300
        ? observers.observe(
          testCase.url,
          new Headers(testCase.headers),
          testCase.response,
          testCase.request,
          testCase.request_headers,
          testCase.method,
        )
        : [];
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
        expect(observed[index].providerRegion).toBe(testCase.expected[index].provider_region);
        expect(observed[index].providerCostUsd).toBe(testCase.expected[index].provider_cost_usd);
        expect(observed[index].providerCostAmount).toBe(testCase.expected[index].provider_cost_amount);
        expect(observed[index].providerCostCurrency).toBe(testCase.expected[index].provider_cost_currency);
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

  it("owns invalid Azure variants without trusting spoofed suffixes", () => {
    const customCategory =
      "https://api.cognitive.microsofttranslator.com/translate?" +
      "api-version=3.0&to=es&category=customer-model";
    const spoofed =
      "https://resource.cognitiveservices.azure.com.evil.example/" +
      "translator/text/v3.0/translate?api-version=3.0&to=es";

    expect(observers.matches(customCategory)).toBe(false);
    expect(observers.ownsEndpointBoundary(customCategory)).toBe(true);
    expect(observers.matches(spoofed)).toBe(false);
    expect(observers.ownsEndpointBoundary(spoofed)).toBe(false);
  });

  it("captures both sides of paired batch collection observations", () => {
    const url = "https://vision.googleapis.com/v1/images:annotate";
    expect(observers.matches(url)).toBe(true);
    expect(observers.needsRequestBody(url)).toBe(true);
    expect(observers.needsResponseBody(url)).toBe(true);
    expect(observers.ownsEndpointBoundary(`${url}/preview`)).toBe(true);
    expect(observers.matches(`${url}/preview`)).toBe(false);
  });
});
