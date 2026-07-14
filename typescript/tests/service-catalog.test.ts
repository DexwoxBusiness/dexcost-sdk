/**
 * Tests for the ServiceCatalog — service price loading, domain matching,
 * endpoint matching, cost extraction, transforms, and user overrides.
 */

import { describe, it, expect, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ServiceCatalog } from "../src/pricing/service-catalog.js";

describe("ServiceCatalog", () => {
  // -----------------------------------------------------------------------
  // Loading
  // -----------------------------------------------------------------------

  it("loads bundled catalog without error", () => {
    const catalog = new ServiceCatalog();
    expect(catalog.catalogVersion).toBeTruthy();
    expect(catalog.catalogVersion.length).toBe(12);
  });

  // -----------------------------------------------------------------------
  // Domain matching — exact
  // -----------------------------------------------------------------------

  it("matches exact domain", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.tavily.com/search");
    expect(entry).not.toBeNull();
    expect(entry!.display_name).toBe("Tavily Search");
  });

  it("matches multiple exact domains", () => {
    const catalog = new ServiceCatalog();
    // Clearbit has multiple exact domains
    const entry1 = catalog.lookup("https://person.clearbit.com/v2/people/find");
    expect(entry1).not.toBeNull();
    expect(entry1!.display_name).toBe("Clearbit");

    const entry2 = catalog.lookup("https://company.clearbit.com/v2/companies/find");
    expect(entry2).not.toBeNull();
    expect(entry2!.display_name).toBe("Clearbit");
  });

  // -----------------------------------------------------------------------
  // Domain matching — wildcard
  // -----------------------------------------------------------------------

  it("matches wildcard domain pattern", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://my-index-abc.pinecone.io/query");
    expect(entry).not.toBeNull();
    expect(entry!.display_name).toBe("Pinecone");
  });

  it("does not load safety-disabled synthetic-zero services", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://myproject.supabase.co/rest/v1/todos");
    expect(entry).toBeNull();
  });

  it("matches wildcard domain for S3", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://mybucket.s3.amazonaws.com/object-key");
    expect(entry).not.toBeNull();
    expect(entry!.display_name).toBe("AWS S3");
  });

  // -----------------------------------------------------------------------
  // Endpoint matching
  // -----------------------------------------------------------------------

  it("matches endpoint-specific entries", () => {
    const catalog = new ServiceCatalog();
    // Google Maps has multiple endpoints on the same domain
    const geocode = catalog.lookup("https://maps.googleapis.com/maps/api/geocode/json?address=NYC");
    expect(geocode).not.toBeNull();
    expect(geocode!.display_name).toBe("Google Maps Geocoding");

    const places = catalog.lookup("https://maps.googleapis.com/maps/api/place/nearbysearch");
    expect(places).not.toBeNull();
    expect(places!.display_name).toBe("Google Maps Places");
  });

  it("returns null for domain with endpoints when no endpoint matches", () => {
    const catalog = new ServiceCatalog();
    // maps.googleapis.com has entries but only specific endpoints
    const entry = catalog.lookup("https://maps.googleapis.com/maps/api/something_else");
    expect(entry).toBeNull();
  });

  // -----------------------------------------------------------------------
  // Unknown domain
  // -----------------------------------------------------------------------

  it("returns null for unknown domain", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.unknown-service.com/v1/data");
    expect(entry).toBeNull();
  });

  it("returns null for unparseable URL", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("not-a-url");
    expect(entry).toBeNull();
  });

  // -----------------------------------------------------------------------
  // Cost extraction: fixed
  // -----------------------------------------------------------------------

  it("extracts fixed cost", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.exa.ai/search");
    expect(entry).not.toBeNull();

    const result = catalog.extractCost(entry!, new Headers(), null);
    expect(result).not.toBeNull();
    expect(result!.costUsd).toBe(0.007);
    expect(result!.confidence).toBe("computed");
    expect(result!.serviceName).toBe("Exa Search");
    expect(result!.pricingSource).toBe("service_catalog");
  });

  // -----------------------------------------------------------------------
  // Cost extraction: endpoint_match
  // -----------------------------------------------------------------------

  it("extracts endpoint_match cost", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://maps.googleapis.com/maps/api/geocode/json");
    expect(entry).not.toBeNull();

    const result = catalog.extractCost(entry!, new Headers(), null);
    expect(result).not.toBeNull();
    expect(result!.costUsd).toBe(0.005);
    expect(result!.confidence).toBe("computed");
  });

  // -----------------------------------------------------------------------
  // Cost extraction: response_body
  // -----------------------------------------------------------------------

  it("extracts cost from response body (credits)", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.tavily.com/search");
    expect(entry).not.toBeNull();

    const body = { results: [], usage: { credits: 3 } };
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // 3 credits * $0.008 per credit = $0.024
    expect(result!.costUsd).toBeCloseTo(0.024, 6);
    expect(result!.confidence).toBe("exact");
  });

  it("falls back to default credits when body field is missing", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.tavily.com/search");
    expect(entry).not.toBeNull();

    // Body doesn't contain api_credits_used
    const body = { results: [] };
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // fallback_credits = 1, cost_per_credit = $0.008
    expect(result!.costUsd).toBeCloseTo(0.008, 6);
    expect(result!.confidence).toBe("estimated");
  });

  it("extracts cost from nested response body path", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.apify.com/v2/acts/run");
    expect(entry).not.toBeNull();

    const body = { data: { stats: { computeUnits: 2.5 } } };
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // 2.5 * $0.25 = $0.625
    expect(result!.costUsd).toBeCloseTo(0.625, 6);
    expect(result!.confidence).toBe("exact");
  });

  // -----------------------------------------------------------------------
  // Cost extraction: response_header
  // -----------------------------------------------------------------------

  it("extracts cost from response header", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://app.scrapingbee.com/api/v1/");
    expect(entry).not.toBeNull();

    const headers = new Headers({ "Spb-cost": "5" });
    const result = catalog.extractCost(entry!, headers, null);
    expect(result).not.toBeNull();
    // 5 credits * $0.000327 = $0.001635
    expect(result!.costUsd).toBeCloseTo(0.001635, 6);
    expect(result!.confidence).toBe("exact");
  });

  it("extracts cost from pinecone read units in body", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://my-index.pinecone.io/query");
    expect(entry).not.toBeNull();

    const body = { usage: { readUnits: 10 }, matches: [] };
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // 10 * $0.000016 = $0.00016
    expect(result!.costUsd).toBeCloseTo(0.00016, 8);
    expect(result!.confidence).toBe("exact");
  });

  // -----------------------------------------------------------------------
  // Transforms
  // -----------------------------------------------------------------------

  it("applies ms_to_seconds transform", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.e2b.dev/sandboxes");
    expect(entry).not.toBeNull();

    const body = { duration_ms: 5000 }; // 5 seconds
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // 5 seconds * $0.000014/sec = $0.000070
    expect(result!.costUsd).toBeCloseTo(0.00007, 6);
    expect(result!.confidence).toBe("exact");
  });

  it("applies ms_to_minutes transform", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.browserbase.com/v1/sessions/abc/run");
    expect(entry).not.toBeNull();

    const body = { duration_ms: 120000 }; // 2 minutes
    const result = catalog.extractCost(entry!, new Headers(), body);
    expect(result).not.toBeNull();
    // 2 minutes * $0.002/min = $0.004
    expect(result!.costUsd).toBeCloseTo(0.004, 6);
    expect(result!.confidence).toBe("exact");
  });

  it("does not load services whose billing lifecycle is not observable", () => {
    const catalog = new ServiceCatalog();
    const entry = catalog.lookup("https://api.stripe.com/v1/charges");
    expect(entry).toBeNull();
  });

  it("bundles the byte-identical safety-filtered canonical catalog", () => {
    const typescriptCatalog = readFileSync(resolve("src/data/service_prices.json"));
    const pythonCanonical = readFileSync(resolve("../python/src/dexcost/data/service_prices.json"));
    expect(typescriptCatalog.equals(pythonCanonical)).toBe(true);
  });

  // -----------------------------------------------------------------------
  // User override precedence
  // -----------------------------------------------------------------------

  it("user override takes precedence over catalog extraction", () => {
    const catalog = new ServiceCatalog();
    catalog.registerOverride("exa_search", 0.05, "request");

    const entry = catalog.lookup("https://api.exa.ai/search");
    expect(entry).not.toBeNull();

    const result = catalog.extractCost(entry!, new Headers(), null);
    expect(result).not.toBeNull();
    expect(result!.costUsd).toBe(0.05);
    expect(result!.pricingSource).toBe("user_override");
  });

  // -----------------------------------------------------------------------
  // Catalog version
  // -----------------------------------------------------------------------

  it("catalog version is a deterministic 12-char hex string", () => {
    const catalog1 = new ServiceCatalog();
    const catalog2 = new ServiceCatalog();
    expect(catalog1.catalogVersion).toBe(catalog2.catalogVersion);
    expect(catalog1.catalogVersion).toMatch(/^[0-9a-f]{12}$/);
  });

  it("authenticates and atomically replaces from a conformant control-plane envelope", async () => {
    const envelope = {
      data: {
        _meta: {
          version: "test",
          service_count: 1,
          disabled_service_count: 1,
          safety_policy_version: "2026-07-14.2",
        },
        custom_search: {
          display_name: "Custom Search",
          domains: ["api.custom-search.test"],
          category: "search",
          pricing_model: "per_request",
          cost_per_request_usd: "0.01",
          cost_extraction: { type: "fixed" },
          source: "test",
          last_verified: "2026-07-14",
        },
      },
      meta: {
        catalog_version: "test",
        safety_policy_version: "2026-07-14.2",
        source: "bundled",
        service_count: 1,
        disabled_service_count: 1,
        disabled_entries: [{ service_key: "unsafe_service" }],
      },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(envelope), { status: 200 }));
    const catalog = new ServiceCatalog();

    await expect(
      catalog.refreshFromUrl("https://api.dexcost.test/v1/api/service-catalog/latest", "dx_test_key"),
    ).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.dexcost.test/v1/api/service-catalog/latest",
      { headers: { Authorization: "Bearer dx_test_key" }, redirect: "error" },
    );
    expect(catalog.lookup("https://api.tavily.com/search")).toBeNull();
    expect(catalog.lookup("https://api.custom-search.test/search")?.display_name).toBe(
      "Custom Search",
    );
    fetchMock.mockRestore();
  });

  it("rejects unsafe or unsupported-policy refreshes without mutation", async () => {
    const invalidEnvelope = {
      data: {
        _meta: {
          version: "test",
          service_count: 1,
          disabled_service_count: 0,
          safety_policy_version: "2026-07-14.2",
        },
        synthetic_zero: {
          display_name: "Synthetic Zero",
          domains: ["zero.test"],
          category: "test",
          pricing_model: "per_request",
          cost_per_request_usd: "0",
          cost_extraction: { type: "fixed" },
          source: "test",
          last_verified: "2026-07-14",
        },
      },
      meta: {
        catalog_version: "test",
        safety_policy_version: "2026-07-14.2",
        source: "bundled",
        service_count: 1,
        disabled_service_count: 0,
        disabled_entries: [],
      },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => new Response(JSON.stringify(invalidEnvelope), { status: 200 }));
    const catalog = new ServiceCatalog();
    const versionBefore = catalog.catalogVersion;

    await expect(catalog.refreshFromUrl("https://api.dexcost.test/catalog")).resolves.toBe(false);
    expect(catalog.catalogVersion).toBe(versionBefore);
    expect(catalog.lookup("https://api.tavily.com/search")).not.toBeNull();
    expect(catalog.lookup("https://zero.test")).toBeNull();

    invalidEnvelope.data.synthetic_zero.cost_per_request_usd = "0.01";
    invalidEnvelope.data._meta.safety_policy_version = "future-policy";
    invalidEnvelope.meta.safety_policy_version = "future-policy";
    await expect(catalog.refreshFromUrl("https://api.dexcost.test/catalog")).resolves.toBe(false);
    expect(catalog.catalogVersion).toBe(versionBefore);
    fetchMock.mockRestore();
  });
});
