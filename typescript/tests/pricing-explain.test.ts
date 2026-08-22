import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { Decimal, createCostEvent } from "../src/core/models.js";
import { CostTracker } from "../src/core/tracker.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  PricingProvenance, explainEventPricing, registerPricingProvenance,
} from "../src/pricing/explain.js";

const directories: string[] = [];
afterEach(() => { for (const path of directories.splice(0)) rmSync(path, { recursive: true, force: true }); });
function buffer(): EventBuffer {
  const path = mkdtempSync(join(tmpdir(), "dexcost-explain-")); directories.push(path);
  return new EventBuffer(join(path, "events.db"));
}
function provenance(release = 42): PricingProvenance {
  return new PricingProvenance({
    catalogSource: "active", stale: false, releaseId: `catalog-release-test-${release}`,
    releaseSequence: release, artifactKind: "llm_prices", artifactSha256: "a".repeat(64),
    artifactSchemaVersion: "1", safetyPolicyVersion: "catalog-safety-v1",
  });
}

describe("local pricing explanations", () => {
  it("snapshots exact catalog provenance without retaining content", () => {
    const version = "catalog-release:42:aaaaaaaaaaaa";
    registerPricingProvenance(version, provenance());
    const storage = buffer();
    const event = createCostEvent({
      eventId: "55555555-5555-4555-8555-555555555555",
      taskId: "11111111-1111-4111-8111-111111111111",
      eventType: "llm_call", provider: "openai", model: "gpt-5",
      inputTokens: 100, outputTokens: 25, cachedTokens: 10,
      costUsd: new Decimal("0.0125"), costConfidence: "computed",
      pricingSource: "litellm", pricingVersion: version,
      details: { prompt: "must never enter explanation" },
    });
    storage.addEvent(event);
    registerPricingProvenance(version, provenance(99));
    const persisted = storage.getEvent(event.eventId)!;
    const explanation = explainEventPricing(persisted);
    expect(explanation.status).toBe("provisional");
    expect(explanation.provenance?.releaseSequence).toBe(42);
    expect(Object.fromEntries(explanation.inputs)).toEqual({
      cached_tokens: "10", input_tokens: "100", model: "gpt-5", output_tokens: "25",
    });
    expect(JSON.stringify(explanation.toDict())).not.toContain("must never enter explanation");
    storage.close();
  });

  it("marks unpriced and provider-reported evidence explicitly", () => {
    const unpriced = createCostEvent({
      eventId: "55555555-5555-4555-8555-555555555556",
      taskId: "11111111-1111-4111-8111-111111111111",
      costConfidence: "unknown", pricingSource: "unknown",
    });
    const exact = createCostEvent({
      eventId: "55555555-5555-4555-8555-555555555557",
      taskId: "11111111-1111-4111-8111-111111111111",
      costUsd: "0.01", pricingSource: "provider_response",
    });
    expect(explainEventPricing(unpriced).status).toBe("unpriced");
    expect(explainEventPricing(exact).status).toBe("provider_reported");
  });

  it("resolves by durable event id and enforces tracked-task ownership", () => {
    const storage = buffer();
    const tracker = new CostTracker({ dbPath: join(directories.at(-1)!, "tracker.db"), autoInstrument: [], storage: "local" });
    const first = tracker.startTask({ taskType: "one" });
    const event = first.recordCost("maps", "0.01");
    expect(first.explainPricing(event.eventId).eventId).toBe(event.eventId);
    const second = tracker.startTask({ taskType: "two" });
    expect(() => second.explainPricing(event)).toThrow(/does not belong/);
    expect(() => tracker.explainPricing("99999999-9999-4999-8999-999999999999")).toThrow(/was not found/);
    first.end(); second.end(); tracker.close(); storage.close();
  });
});
