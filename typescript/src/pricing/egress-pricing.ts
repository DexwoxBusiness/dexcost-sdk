/**
 * Egress pricing engine — resolves a per-GB egress rate from
 * `(provider, region)` using the bundled `data/egress_prices.json` catalog.
 *
 * Mirrors the Python `dexcost.egress_pricing` module in shape and contract:
 * bundled JSON + a resolver that returns `(ratePerGb, pricingSource,
 * costConfidence)` via the spec §7.1 5-tier degradation ladder.
 *
 * Fail-silent contract: every failure mode degrades through the ladder;
 * the engine always returns a usable `EgressRate`.
 *
 * Numeric precision note: the TypeScript SDK uses `number` for cost fields,
 * so rates are stored as strings (preserving catalog precision) and parsed
 * only at the boundary. Catalog string values match the Python Decimal
 * equivalents exactly — see `tests/egress-pricing.test.ts`.
 */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Tier-4 ultimate fallback — used only when the catalog cannot be read at
 *  all AND `_meta.default_rate_usd_per_gb` cannot be resolved. */
const HARDCODED_DEFAULT = "0.09";

// ---------------------------------------------------------------------------
// Warn-once tracking (module-level state)
// ---------------------------------------------------------------------------

const _warnedModes: Set<string> = new Set();

function _warnOnce(mode: string, message: string): void {
  if (_warnedModes.has(mode)) return;
  _warnedModes.add(mode);
  // eslint-disable-next-line no-console
  console.warn(`[dexcost.egress-pricing] ${message}`);
}

/** Test-only: clear warn-once tracking. */
export function _resetEgressWarningStateForTests(): void {
  _warnedModes.clear();
}

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface EgressRate {
  /** Rate string from the catalog (e.g. "0.09", "0.1093"); caller parses. */
  ratePerGb: string;
  /** Audit-trail identifier (e.g. "egress_catalog:aws:us-east-1"). */
  pricingSource: string;
  /** Confidence band: "exact" | "computed" | "estimated". */
  costConfidence: "exact" | "computed" | "estimated";
}

interface ProviderBlock {
  default_usd_per_gb?: string | number;
  regions?: Record<string, string | number>;
  _last_verified?: string;
}

interface CatalogMeta {
  version?: string;
  last_updated?: string;
  currency?: string;
  default_rate_usd_per_gb?: string | number;
}

interface CatalogJson {
  _meta?: CatalogMeta;
  [provider: string]: ProviderBlock | CatalogMeta | undefined;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Validate that a string/number is a usable decimal rate. Returns the
 * normalised string form, or null if it cannot be parsed.
 *
 * We preserve the original string when possible to avoid float drift in
 * stored data. For numbers we stringify via `String(v)` (catalog should
 * always use strings, but this provides a safety net).
 */
function _normaliseRate(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    return String(value);
  }
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number.parseFloat(trimmed);
  if (Number.isNaN(parsed) || !Number.isFinite(parsed)) return null;
  // Preserve the original (string) representation — no float drift.
  return trimmed;
}

// ---------------------------------------------------------------------------
// EgressPricingEngine
// ---------------------------------------------------------------------------

export class EgressPricingEngine {
  private _catalog: CatalogJson = {};
  private _catalogVersion: string = "unknown";

  /**
   * @param catalogJson Optional override — already-parsed JSON object. When
   *   omitted, the bundled `data/egress_prices.json` is loaded.
   */
  constructor(catalogJson?: object) {
    if (catalogJson !== undefined) {
      // Accept already-parsed JSON object (used by tests).
      try {
        this._catalog = catalogJson as CatalogJson;
        const meta = this._catalog._meta;
        if (meta && typeof meta === "object") {
          this._catalogVersion = String(meta.version ?? "unknown");
        }
      } catch {
        _warnOnce(
          "catalog_malformed",
          "egress catalog malformed; falling back to hardcoded default",
        );
        this._catalog = {};
      }
      return;
    }
    this._loadBundled();
  }

  /** Load and parse a catalog from disk by file path. */
  static fromPath(catalogPath: string): EgressPricingEngine {
    let raw: string;
    try {
      raw = readFileSync(catalogPath, "utf-8");
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "ENOENT") {
        _warnOnce(
          "catalog_missing",
          `egress catalog file not found (${catalogPath}); ` +
            "falling back to hardcoded default",
        );
      } else {
        _warnOnce(
          "catalog_unreadable",
          `egress catalog unreadable (${String(err)}); ` +
            "falling back to hardcoded default",
        );
      }
      return new EgressPricingEngine({});
    }

    let parsed: CatalogJson;
    try {
      parsed = JSON.parse(raw) as CatalogJson;
    } catch (err) {
      _warnOnce(
        "catalog_malformed",
        `egress catalog malformed JSON (${String(err)}); ` +
          "falling back to hardcoded default",
      );
      return new EgressPricingEngine({});
    }
    return new EgressPricingEngine(parsed);
  }

  private _loadBundled(): void {
    try {
      const req = createRequire(import.meta.url);
      const raw = req("../data/egress_prices.json") as CatalogJson;
      this._catalog = raw;
      const meta = raw._meta;
      if (meta && typeof meta === "object") {
        this._catalogVersion = String(meta.version ?? "unknown");
      }
    } catch (err) {
      _warnOnce(
        "catalog_missing",
        `bundled egress catalog could not be loaded (${String(err)}); ` +
          "falling back to hardcoded default",
      );
      this._catalog = {};
    }
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  get catalogVersion(): string {
    return this._catalogVersion;
  }

  /** Rate for a call classified as internal traffic — always free. */
  rateForInternal(): EgressRate {
    return {
      ratePerGb: "0",
      pricingSource: "egress_catalog:internal",
      costConfidence: "exact",
    };
  }

  /**
   * Resolve an egress rate via the §7.1 degradation ladder.
   *
   * - Tier 1: `(provider, region)` exact match → region rate, `computed`.
   * - Tier 2: provider known, region absent/unknown → provider default,
   *           `estimated`.
   * - Tier 3: provider not detected / not in catalog → `_meta` default,
   *           `estimated`.
   * - Tier 4: catalog unreadable or `_meta` default absent → hardcoded
   *           `"0.09"`, `estimated`.
   */
  resolveRate(provider: string | null, region: string | null): EgressRate {
    if (provider) {
      const block = this._catalog[provider];
      if (block && typeof block === "object" && !this._isMeta(block)) {
        const providerBlock = block as ProviderBlock;
        const regions = providerBlock.regions ?? {};
        if (region && Object.prototype.hasOwnProperty.call(regions, region)) {
          const normalised = _normaliseRate(regions[region]);
          if (normalised !== null) {
            return {
              ratePerGb: normalised,
              pricingSource: `egress_catalog:${provider}:${region}`,
              costConfidence: "computed",
            };
          }
          _warnOnce(
            `region_rate_malformed:${provider}:${region}`,
            `egress region rate malformed for ${provider}/${region}`,
          );
          // Fall through to provider default.
        }
        const provDefault = _normaliseRate(providerBlock.default_usd_per_gb);
        if (provDefault !== null) {
          return {
            ratePerGb: provDefault,
            pricingSource: `egress_catalog:${provider}:default`,
            costConfidence: "estimated",
          };
        }
      }
    }

    const meta = this._catalog._meta;
    if (meta && typeof meta === "object") {
      const metaDefault = _normaliseRate(meta.default_rate_usd_per_gb);
      if (metaDefault !== null) {
        return {
          ratePerGb: metaDefault,
          pricingSource: "egress_catalog:default",
          costConfidence: "estimated",
        };
      }
      _warnOnce(
        "meta_default_missing",
        "egress _meta.default_rate_usd_per_gb missing/malformed; " +
          "using hardcoded default",
      );
    }

    return {
      ratePerGb: HARDCODED_DEFAULT,
      pricingSource: "egress_catalog:default",
      costConfidence: "estimated",
    };
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private _isMeta(block: ProviderBlock | CatalogMeta): boolean {
    // The `_meta` block isn't a provider block. The catalog uses a flat
    // dictionary with provider names as keys and `_meta` as a sibling; we
    // only call this on keys other than `_meta`, but defensively guard
    // against shapes that look meta-ish.
    return "default_rate_usd_per_gb" in block;
  }
}
