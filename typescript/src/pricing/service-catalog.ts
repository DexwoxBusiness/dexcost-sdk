/**
 * Service Catalog — cost extraction engine for non-LLM services.
 *
 * Loads the bundled service_prices.json catalog and matches HTTP requests
 * against known service domains to extract per-request costs automatically.
 *
 * Implements US-035 service catalog cost extraction.
 */

import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

// ---------------------------------------------------------------------------
// Public interfaces
// ---------------------------------------------------------------------------

/** A cost extraction definition from the catalog. */
export interface CostExtractionDef {
  type: "response_body" | "response_header" | "endpoint_match" | "fixed";
  path?: string;
  header?: string;
  transform?: string;
  fallback_credits?: number;
  units?: number;
}

/** A single service entry from the catalog JSON. */
export interface ServiceEntry {
  display_name: string;
  domains: string[];
  endpoints?: string[];
  category: string;
  pricing_model: string;
  cost_extraction: CostExtractionDef;
  source: string;
  last_verified: string;
  note?: string;
  // Dynamic cost fields (vary by pricing_model)
  cost_per_credit_usd?: string;
  default_credits_per_request?: number;
  cost_per_request_usd?: string;
  cost_per_page_usd?: string;
  cost_per_minute_usd?: string;
  cost_per_compute_unit_usd?: string;
  cost_per_message_usd?: string;
  cost_per_email_usd?: string;
  cost_per_second_usd?: string;
  cost_per_search_usd?: string;
  cost_per_query_usd?: string;
  cost_per_read_unit_usd?: string;
  cost_per_1k_characters_usd?: string;
  percentage?: string;
  fixed_fee_usd?: string;
}

/** Result of extracting cost from a service response. */
export interface CostExtractionResult {
  costUsd: number;
  confidence: string;
  serviceName: string;
  pricingSource: string;
}

/** Catalog metadata. */
interface CatalogMeta {
  version: string;
  last_updated: string;
  description: string;
  how_cost_is_extracted: Record<string, string>;
}

type CatalogJson = {
  _meta: CatalogMeta;
} & Record<string, ServiceEntry | CatalogMeta>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Check if a hostname matches a domain pattern (supports `*.example.com` wildcards).
 */
function domainMatches(hostname: string, pattern: string): boolean {
  if (pattern.startsWith("*.")) {
    const suffix = pattern.slice(1); // ".example.com"
    return hostname.endsWith(suffix) || hostname === pattern.slice(2);
  }
  return hostname === pattern;
}

/**
 * Navigate a nested object by dot-separated path (e.g. "data.stats.computeUnits").
 */
function getNestedValue(obj: unknown, path: string): unknown {
  let current: unknown = obj;
  for (const key of path.split(".")) {
    if (current === null || current === undefined || typeof current !== "object") {
      return undefined;
    }
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

/**
 * Apply a named transform to a raw numeric value + service entry to get costUsd.
 */
function applyTransform(
  transform: string,
  rawValue: number,
  entry: ServiceEntry
): number | null {
  switch (transform) {
    case "ms_to_seconds": {
      const seconds = rawValue / 1000;
      const rate = parseFloat(entry.cost_per_second_usd ?? "0");
      if (isNaN(rate)) return null;
      return seconds * rate;
    }
    case "ms_to_minutes": {
      const minutes = rawValue / 60000;
      const rate = parseFloat(entry.cost_per_minute_usd ?? "0");
      if (isNaN(rate)) return null;
      return minutes * rate;
    }
    case "stripe_fee": {
      // rawValue is amount in cents; Stripe charges 2.9% + $0.30
      const percentage = parseFloat(entry.percentage ?? "0.029");
      if (isNaN(percentage)) return null;
      const fixedFee = parseFloat(entry.fixed_fee_usd ?? "0.30");
      if (isNaN(fixedFee)) return null;
      const amountUsd = rawValue / 100;
      return amountUsd * percentage + fixedFee;
    }
    default:
      return rawValue;
  }
}

/**
 * Get the fixed per-unit cost from a service entry.
 */
function getFixedCost(entry: ServiceEntry): number | null {
  // Try each known cost field
  if (entry.cost_per_request_usd !== undefined) {
    const v = parseFloat(entry.cost_per_request_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_page_usd !== undefined) {
    const v = parseFloat(entry.cost_per_page_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_search_usd !== undefined) {
    const v = parseFloat(entry.cost_per_search_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_query_usd !== undefined) {
    const v = parseFloat(entry.cost_per_query_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_message_usd !== undefined) {
    const v = parseFloat(entry.cost_per_message_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_email_usd !== undefined) {
    const v = parseFloat(entry.cost_per_email_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_second_usd !== undefined) {
    const v = parseFloat(entry.cost_per_second_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_minute_usd !== undefined) {
    const v = parseFloat(entry.cost_per_minute_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_credit_usd !== undefined) {
    const credits = entry.default_credits_per_request ?? 1;
    const v = parseFloat(entry.cost_per_credit_usd);
    return isNaN(v) ? null : v * credits;
  }
  if (entry.cost_per_read_unit_usd !== undefined) {
    const v = parseFloat(entry.cost_per_read_unit_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_compute_unit_usd !== undefined) {
    const v = parseFloat(entry.cost_per_compute_unit_usd);
    return isNaN(v) ? null : v;
  }
  if (entry.cost_per_1k_characters_usd !== undefined) {
    const v = parseFloat(entry.cost_per_1k_characters_usd);
    return isNaN(v) ? null : v;
  }
  return 0;
}

// ---------------------------------------------------------------------------
// ServiceCatalog
// ---------------------------------------------------------------------------

export class ServiceCatalog {
  private _entries: Map<string, ServiceEntry> = new Map();
  private _overrides: Map<string, { costPerUnit: number; per: string }> = new Map();
  private _version: string;

  constructor(catalogPath?: string) {
    let raw: CatalogJson;
    if (catalogPath) {
      try {
        const content = readFileSync(catalogPath, "utf-8");
        raw = JSON.parse(content) as CatalogJson;
      } catch {
        // Fall back to bundled catalog on any error (missing file, corrupt JSON)
        const req = createRequire(import.meta.url);
        raw = req("../data/service_prices.json") as CatalogJson;
      }
    } else {
      const req = createRequire(import.meta.url);
      raw = req("../data/service_prices.json") as CatalogJson;
    }
    this._loadFromJson(raw);
    this._version = createHash("sha256")
      .update(JSON.stringify(raw))
      .digest("hex")
      .slice(0, 12);
  }

  /** Deterministic hash of the loaded catalog. */
  get catalogVersion(): string {
    return this._version;
  }

  /**
   * Look up a service entry by URL.
   *
   * Domain matching supports wildcards (e.g. `*.pinecone.io`).
   * Endpoint matching checks if the URL pathname starts with any entry endpoint.
   */
  lookup(url: string): ServiceEntry | null {
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(url);
    } catch {
      return null;
    }

    const hostname = parsedUrl.hostname;
    const pathname = parsedUrl.pathname;

    // First pass: find all entries whose domain matches
    const candidates: Array<{ key: string; entry: ServiceEntry }> = [];
    for (const [key, entry] of this._entries) {
      for (const domain of entry.domains) {
        if (domainMatches(hostname, domain)) {
          candidates.push({ key, entry });
          break;
        }
      }
    }

    if (candidates.length === 0) return null;

    // If any candidate has endpoint restrictions, prefer exact endpoint match
    for (const { entry } of candidates) {
      if (entry.endpoints && entry.endpoints.length > 0) {
        for (const ep of entry.endpoints) {
          if (pathname.startsWith(ep)) {
            return entry;
          }
        }
      }
    }

    // Return first candidate without endpoint restriction
    for (const { entry } of candidates) {
      if (!entry.endpoints || entry.endpoints.length === 0) {
        return entry;
      }
    }

    // All candidates had endpoints but none matched
    return null;
  }

  /**
   * Extract cost from a service response.
   *
   * @param entry    The matched ServiceEntry from lookup()
   * @param headers  Response headers
   * @param body     Parsed response body (or null/undefined if not available)
   */
  extractCost(
    entry: ServiceEntry,
    headers: Headers,
    body: unknown
  ): CostExtractionResult | null {
    const extraction = entry.cost_extraction;
    const serviceName = entry.display_name;

    // Check for user override first
    for (const [key, override] of this._overrides) {
      const catalogEntry = this._entries.get(key);
      if (catalogEntry && catalogEntry.display_name === serviceName) {
        return {
          costUsd: override.costPerUnit,
          confidence: "computed",
          serviceName,
          pricingSource: "user_override",
        };
      }
    }

    switch (extraction.type) {
      case "response_body": {
        if (body === null || body === undefined) {
          // Fall back to defaults
          return this._fallbackResult(entry, serviceName);
        }
        const path = extraction.path;
        if (!path) return this._fallbackResult(entry, serviceName);

        const rawValue = getNestedValue(body, path);
        if (rawValue === undefined || rawValue === null) {
          return this._fallbackResult(entry, serviceName);
        }

        const numValue = typeof rawValue === "number" ? rawValue : parseFloat(String(rawValue));
        if (isNaN(numValue)) return this._fallbackResult(entry, serviceName);

        let costUsd: number | null;
        if (extraction.transform) {
          costUsd = applyTransform(extraction.transform, numValue, entry);
        } else {
          // Multiply raw value by per-unit cost
          costUsd = this._computeCostFromUnits(numValue, entry);
        }

        if (costUsd === null) return null;

        return {
          costUsd,
          confidence: "exact",
          serviceName,
          pricingSource: "service_catalog",
        };
      }

      case "response_header": {
        const headerName = extraction.header;
        if (!headerName) return this._fallbackResult(entry, serviceName);

        const headerValue = headers.get(headerName);
        if (headerValue === null) return this._fallbackResult(entry, serviceName);

        const numValue = parseFloat(headerValue);
        if (isNaN(numValue)) return null;

        const costUsd = this._computeCostFromUnits(numValue, entry);
        if (costUsd === null) return null;
        return {
          costUsd,
          confidence: "exact",
          serviceName,
          pricingSource: "service_catalog",
        };
      }

      case "endpoint_match":
      case "fixed": {
        const costUsd = getFixedCost(entry);
        if (costUsd === null) return null;
        return {
          costUsd,
          confidence: "computed",
          serviceName,
          pricingSource: "service_catalog",
        };
      }

      default:
        return null;
    }
  }

  /**
   * Register a user override for a service key. Overrides take precedence
   * over catalog-extracted costs.
   */
  registerOverride(serviceKey: string, costPerUnit: number, per: string): void {
    this._overrides.set(serviceKey, { costPerUnit, per });
  }

  /**
   * Refresh the catalog from a remote URL.
   */
  async refreshFromUrl(url: string): Promise<void> {
    try {
      const response = await fetch(url);
      if (!response.ok) return;
      const text = await response.text();
      let raw: CatalogJson;
      try {
        raw = JSON.parse(text) as CatalogJson;
      } catch {
        return; // Malformed JSON — keep using existing catalog
      }
      this._entries.clear();
      this._loadFromJson(raw);
      this._version = createHash("sha256")
        .update(JSON.stringify(raw))
        .digest("hex")
        .slice(0, 12);
    } catch {
      // Network error — keep using existing catalog
    }
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private _loadFromJson(raw: CatalogJson): void {
    for (const [key, value] of Object.entries(raw)) {
      if (key === "_meta") continue;
      this._entries.set(key, value as ServiceEntry);
    }
  }

  private _computeCostFromUnits(units: number, entry: ServiceEntry): number | null {
    if (entry.cost_per_credit_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_credit_usd);
      if (isNaN(rate)) return null;
      return units * rate;
    }
    if (entry.cost_per_read_unit_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_read_unit_usd);
      if (isNaN(rate)) return null;
      return units * rate;
    }
    if (entry.cost_per_compute_unit_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_compute_unit_usd);
      if (isNaN(rate)) return null;
      return units * rate;
    }
    if (entry.cost_per_1k_characters_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_1k_characters_usd);
      if (isNaN(rate)) return null;
      return (units / 1000) * rate;
    }
    if (entry.cost_per_minute_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_minute_usd);
      if (isNaN(rate)) return null;
      return units * rate;
    }
    // Default: multiply units by fixed cost
    const fixedCost = getFixedCost(entry);
    if (fixedCost === null) return null;
    return units * fixedCost;
  }

  private _fallbackResult(entry: ServiceEntry, serviceName: string): CostExtractionResult | null {
    const extraction = entry.cost_extraction;
    if (extraction.fallback_credits !== undefined && entry.cost_per_credit_usd !== undefined) {
      const rate = parseFloat(entry.cost_per_credit_usd);
      if (isNaN(rate)) return null;
      return {
        costUsd: extraction.fallback_credits * rate,
        confidence: "estimated",
        serviceName,
        pricingSource: "service_catalog",
      };
    }
    // Fall back to fixed cost
    const costUsd = getFixedCost(entry);
    if (costUsd === null) return null;
    return {
      costUsd,
      confidence: costUsd > 0 ? "estimated" : "unknown",
      serviceName,
      pricingSource: "service_catalog",
    };
  }
}
