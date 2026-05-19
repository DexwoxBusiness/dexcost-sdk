import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { PricingEngine } from "../src/pricing/engine.js";

describe("PricingEngine", () => {
  it("calculates cost for known model", () => {
    const engine = new PricingEngine();
    const result = engine.getCost("gpt-4o", 1000, 500);
    expect(result.costUsd).toBeGreaterThan(0);
    expect(result.pricingSource).toBe("litellm");
    expect(result.costConfidence).toBe("computed");
    expect(result.pricingVersion).toBeTruthy();
  });

  it("returns zero cost for unknown model", () => {
    const engine = new PricingEngine();
    const result = engine.getCost("totally-fake-model", 1000, 500);
    expect(result.costUsd).toBe(0);
    expect(result.pricingSource).toBe("unknown");
    expect(result.costConfidence).toBe("unknown");
  });

  it("resolves model with provider prefix", () => {
    const engine = new PricingEngine();
    const result = engine.getCost("openai/gpt-4o", 1000, 500);
    expect(result.costUsd).toBeGreaterThan(0);
    expect(result.pricingSource).toBe("litellm");
  });

  it("resolves model without date suffix", () => {
    const engine = new PricingEngine();
    const withDate = engine.getCost("gpt-4o-2024-08-06", 1000, 500);
    expect(withDate.costUsd).toBeGreaterThan(0);
  });

  it("handles custom pricing override", () => {
    const engine = new PricingEngine();
    engine.setCustomPricing("my-finetune", 0.005, 0.015);
    const result = engine.getCost("my-finetune", 1000, 500);
    expect(result.costUsd).toBeCloseTo(0.0125, 6);
    expect(result.pricingSource).toBe("custom");
    expect(result.costConfidence).toBe("computed");
  });

  it("custom pricing takes precedence over bundled", () => {
    const engine = new PricingEngine();
    engine.setCustomPricing("gpt-4o", 0.001, 0.001);
    const result = engine.getCost("gpt-4o", 1000, 1000);
    expect(result.costUsd).toBeCloseTo(0.002, 6);
    expect(result.pricingSource).toBe("custom");
  });

  it("handles cached tokens discount", () => {
    const engine = new PricingEngine();
    const noCached = engine.getCost("gpt-4o", 1000, 500, 0);
    const withCached = engine.getCost("gpt-4o", 1000, 500, 500);
    expect(withCached.costUsd).toBeLessThanOrEqual(noCached.costUsd);
  });

  it("pricing version is stable 12-char hash", () => {
    const engine = new PricingEngine();
    expect(engine.pricingVersion).toBeTruthy();
    expect(engine.pricingVersion.length).toBe(12);
  });
});

describe("PricingEngine background refresh", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("refreshFromServer updates model map on success", async () => {
    const engine = new PricingEngine();
    const originalVersion = engine.pricingVersion;

    const mockModels = {
      "test-refresh-model": {
        input_cost_per_token: 0.00001,
        output_cost_per_token: 0.00003,
      },
    };

    // Control Layer contract: models nested under data.data, with the
    // pricing_version alongside.
    const payload = {
      data: { data: mockModels, pricing_version: "server-v-42" },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        text: async () => JSON.stringify(payload),
        json: async () => payload,
      })
    );

    await engine.refreshFromServer("https://example.com");

    const result = engine.getCost("test-refresh-model", 1000, 500);
    expect(result.costUsd).toBeGreaterThan(0);
    expect(result.pricingSource).toBe("litellm");
    expect(engine.pricingVersion).not.toBe(originalVersion);
    // pricing_version from the server payload is captured verbatim.
    expect(engine.pricingVersion).toBe("server-v-42");
  });

  it("refreshFromServer is fail-silent on network error", async () => {
    const engine = new PricingEngine();
    const originalVersion = engine.pricingVersion;

    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("Network failure"))
    );

    await expect(
      engine.refreshFromServer("https://example.com")
    ).resolves.toBeUndefined();

    expect(engine.pricingVersion).toBe(originalVersion);
  });

  it("refreshFromServer is fail-silent on non-200 response", async () => {
    const engine = new PricingEngine();
    const originalVersion = engine.pricingVersion;

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
      })
    );

    await expect(
      engine.refreshFromServer("https://example.com")
    ).resolves.toBeUndefined();

    expect(engine.pricingVersion).toBe(originalVersion);
  });

  it("startBackgroundRefresh calls fetch immediately and on interval", async () => {
    vi.useFakeTimers();

    const engine = new PricingEngine();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ models: {} }),
      })
    );

    engine.startBackgroundRefresh("https://example.com", 5000);

    // Let the immediate fire-and-forget Promise resolve (no timers triggered)
    await Promise.resolve();
    await Promise.resolve();

    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance by one interval
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Advance by another interval
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    engine.stopBackgroundRefresh();
    vi.useRealTimers();
  });

  it("stopBackgroundRefresh clears the interval", async () => {
    vi.useFakeTimers();

    const engine = new PricingEngine();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ models: {} }),
      })
    );

    engine.startBackgroundRefresh("https://example.com", 5000);

    // Let the immediate fire-and-forget Promise resolve (no timers triggered)
    await Promise.resolve();
    await Promise.resolve();

    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const callsAfterStart = fetchMock.mock.calls.length;

    engine.stopBackgroundRefresh();

    // Advance well past the interval — no new calls should happen
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchMock).toHaveBeenCalledTimes(callsAfterStart);

    vi.useRealTimers();
  });
});
