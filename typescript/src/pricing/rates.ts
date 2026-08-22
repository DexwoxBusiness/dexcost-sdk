import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { Decimal } from "../core/models.js";
import type { DecimalLike } from "../core/models.js";

export interface RateEntry {
  service: string;
  per: string;
  costUsd: Decimal;
}

export interface InfrastructureRateEntry {
  kind: "gpu" | "network";
  key: string;
  per: "gpu_second" | "gpu_hour" | "gb_transferred" | "gb_egress";
  costUsd: Decimal;
}

const INFRASTRUCTURE_UNITS = {
  gpu: new Set(["gpu_second", "gpu_hour"]),
  network: new Set(["gb_transferred", "gb_egress"]),
} as const;

export function normalizeInfrastructureKey(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function strictDecimal(value: DecimalLike, path: string): Decimal {
  let amount: Decimal;
  try {
    amount = value instanceof Decimal
      ? value
      : new Decimal(typeof value === "number" ? String(value) : value);
  } catch {
    throw new Error(`${path} must be a finite decimal.`);
  }
  if (!amount.isFinite()) throw new Error(`${path} must be a finite decimal.`);
  return amount;
}

function positiveDecimal(value: DecimalLike, path: string): Decimal {
  const amount = strictDecimal(value, path);
  if (amount.lte(0)) throw new Error(`${path} must be a positive finite decimal.`);
  return amount;
}

function yamlModule(): {
  load: (input: string) => unknown;
  dump: (value: unknown, options?: Record<string, unknown>) => string;
} {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    return require("js-yaml") as ReturnType<typeof yamlModule>;
  } catch {
    throw new Error(
      "The 'js-yaml' package is required for YAML rate loading/export. Install it with: npm install js-yaml",
    );
  }
}

function isMapping(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export class RateRegistry {
  private _rates: Map<string, RateEntry> = new Map();
  private _infrastructureRates: Map<string, InfrastructureRateEntry> = new Map();
  private _version: string | null = null;

  register(service: string, per: string, costUsd: DecimalLike): void {
    this._rates.set(service, {
      service,
      per,
      costUsd: strictDecimal(costUsd, `Rate ${service}.cost_usd`),
    });
    this._version = null;
  }

  get(service: string): RateEntry | undefined {
    return this._rates.get(service);
  }

  registerInfrastructure(
    kind: string,
    key: string,
    per: string,
    costUsd: DecimalLike,
  ): void {
    const normalizedKind = kind.trim().toLowerCase();
    if (normalizedKind !== "gpu" && normalizedKind !== "network") {
      throw new Error("Infrastructure kind must be 'gpu' or 'network'.");
    }
    const normalizedKey = normalizeInfrastructureKey(key);
    if (!normalizedKey) throw new Error("Infrastructure rate key cannot be empty.");
    const normalizedPer = per.trim().toLowerCase();
    const allowed = INFRASTRUCTURE_UNITS[normalizedKind];
    if (!allowed.has(normalizedPer as never)) {
      throw new Error(
        `Infrastructure rate ${normalizedKind}.${normalizedKey}.per must be one of: ${
          Array.from(allowed).sort().join(", ")
        }.`,
      );
    }
    const entry: InfrastructureRateEntry = {
      kind: normalizedKind,
      key: normalizedKey,
      per: normalizedPer as InfrastructureRateEntry["per"],
      costUsd: positiveDecimal(
        costUsd,
        `Infrastructure rate ${normalizedKind}.${normalizedKey}.cost_usd`,
      ),
    };
    this._infrastructureRates.set(`${normalizedKind}:${normalizedKey}`, entry);
    this._version = null;
  }

  getInfrastructure(kind: string, key: string): InfrastructureRateEntry | undefined {
    return this._infrastructureRates.get(
      `${kind.trim().toLowerCase()}:${normalizeInfrastructureKey(key)}`,
    );
  }

  get rates(): Record<string, RateEntry> {
    const copy: Record<string, RateEntry> = {};
    for (const [key, value] of this._rates) copy[key] = { ...value };
    return copy;
  }

  get infrastructureRates(): Record<string, InfrastructureRateEntry> {
    const copy: Record<string, InfrastructureRateEntry> = {};
    for (const [key, value] of this._infrastructureRates) copy[key] = { ...value };
    return copy;
  }

  get pricingVersion(): string {
    if (this._version === null) this._version = this._computeVersion();
    return this._version;
  }

  load(path: string): void {
    let content: string;
    try {
      content = readFileSync(path, "utf-8");
    } catch (error) {
      throw new Error(
        `Cannot read rates file ${path}: ${error instanceof Error ? error.message : error}`,
      );
    }

    let parsed: unknown;
    try {
      parsed = yamlModule().load(content) ?? {};
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("The 'js-yaml'")) throw error;
      throw new Error(
        `Invalid YAML in rates file ${path}: ${error instanceof Error ? error.message : error}`,
      );
    }
    if (!isMapping(parsed)) throw new Error("Expected a mapping at the root of the YAML file.");

    const version = parsed["version"] ?? 1;
    if (typeof version !== "number" || !Number.isInteger(version) || (version !== 1 && version !== 2)) {
      throw new Error("Rates YAML 'version' must be 1 or 2.");
    }
    const ratesData = parsed["rates"] ?? {};
    if (!isMapping(ratesData)) {
      throw new Error("Expected 'rates' key with a mapping in the YAML file.");
    }
    const infrastructureData = parsed["infrastructure"] ?? {};
    if (!isMapping(infrastructureData)) {
      throw new Error("Expected 'infrastructure' to be a mapping in the YAML file.");
    }
    if (Object.keys(infrastructureData).length > 0 && version !== 2) {
      throw new Error("Infrastructure rates require rates YAML version: 2.");
    }
    const unknownKinds = Object.keys(infrastructureData)
      .filter((kind) => kind !== "gpu" && kind !== "network")
      .sort();
    if (unknownKinds.length > 0) {
      throw new Error(`Unsupported infrastructure rate kind(s): ${unknownKinds.join(", ")}.`);
    }

    const pendingRates: Array<[string, string, DecimalLike]> = [];
    for (const [service, info] of Object.entries(ratesData)) {
      if (!isMapping(info) || !("cost_usd" in info)) {
        throw new Error(
          `Rate entry for ${JSON.stringify(service)} must be a mapping with at least 'cost_usd'.`,
        );
      }
      pendingRates.push([service, String(info["per"] ?? "unit"), String(info["cost_usd"])]);
    }

    const pendingInfrastructure: Array<[string, string, string, DecimalLike]> = [];
    for (const [kind, entries] of Object.entries(infrastructureData)) {
      if (!isMapping(entries)) {
        throw new Error(`Expected 'infrastructure.${kind}' to be a mapping.`);
      }
      const normalizedKeys = new Set<string>();
      for (const [key, info] of Object.entries(entries)) {
        const normalizedKey = normalizeInfrastructureKey(key);
        if (normalizedKeys.has(normalizedKey)) {
          throw new Error(
            `Infrastructure rate ${kind}.${key} duplicates normalized key ${JSON.stringify(normalizedKey)}.`,
          );
        }
        normalizedKeys.add(normalizedKey);
        if (!isMapping(info) || !("per" in info) || !("cost_usd" in info)) {
          throw new Error(
            `Infrastructure rate ${kind}.${key} must contain 'per' and 'cost_usd'.`,
          );
        }
        pendingInfrastructure.push([
          kind,
          key,
          String(info["per"]),
          String(info["cost_usd"]),
        ]);
      }
    }

    // Validate everything before mutating this registry. A malformed file
    // cannot leave a partially applied pricing snapshot.
    const validator = new RateRegistry();
    for (const [service, per, costUsd] of pendingRates) validator.register(service, per, costUsd);
    for (const args of pendingInfrastructure) validator.registerInfrastructure(...args);
    for (const [service, per, costUsd] of pendingRates) this.register(service, per, costUsd);
    for (const args of pendingInfrastructure) this.registerInfrastructure(...args);
  }

  export(path: string): void {
    const rates: Record<string, { per: string; cost_usd: string }> = {};
    for (const service of Array.from(this._rates.keys()).sort()) {
      const entry = this._rates.get(service)!;
      rates[service] = { per: entry.per, cost_usd: entry.costUsd.toString() };
    }

    const infrastructure: Record<string, Record<string, { per: string; cost_usd: string }>> = {};
    for (const kind of ["gpu", "network"] as const) {
      const entries: Record<string, { per: string; cost_usd: string }> = {};
      for (const entry of Array.from(this._infrastructureRates.values())
        .filter((candidate) => candidate.kind === kind)
        .sort((a, b) => a.key.localeCompare(b.key))) {
        entries[entry.key] = { per: entry.per, cost_usd: entry.costUsd.toString() };
      }
      if (Object.keys(entries).length > 0) infrastructure[kind] = entries;
    }

    const payload: Record<string, unknown> = { version: 2, rates };
    if (Object.keys(infrastructure).length > 0) payload["infrastructure"] = infrastructure;
    const output = yamlModule().dump(payload, { sortKeys: false, noRefs: true });
    try {
      writeFileSync(path, output, "utf-8");
    } catch (error) {
      throw new Error(
        `Cannot write rates file ${path}: ${error instanceof Error ? error.message : error}`,
      );
    }
  }

  private _computeVersion(): string {
    const parts: string[] = [];
    for (const service of Array.from(this._rates.keys()).sort()) {
      const entry = this._rates.get(service)!;
      parts.push(`${service}:${entry.per}:${entry.costUsd.toString()}`);
    }
    for (const entry of Array.from(this._infrastructureRates.values())
      .sort((a, b) => a.kind.localeCompare(b.kind) || a.key.localeCompare(b.key))) {
      parts.push(
        `infrastructure:${entry.kind}:${entry.key}:${entry.per}:${entry.costUsd.toString()}`,
      );
    }
    return createHash("sha256").update(parts.join("|"), "utf-8").digest("hex").slice(0, 12);
  }
}
