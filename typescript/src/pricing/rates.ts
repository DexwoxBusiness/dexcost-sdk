import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

export interface RateEntry {
  service: string;
  per: string;
  costUsd: number;
}

export class RateRegistry {
  private _rates: Map<string, RateEntry> = new Map();
  private _version: string | null = null;

  register(service: string, per: string, costUsd: number): void {
    this._rates.set(service, { service, per, costUsd });
    this._version = null; // Invalidate cached version hash
  }

  get(service: string): RateEntry | undefined {
    return this._rates.get(service);
  }

  get rates(): Record<string, RateEntry> {
    const copy: Record<string, RateEntry> = {};
    for (const [key, value] of this._rates) {
      copy[key] = { ...value };
    }
    return copy;
  }

  get pricingVersion(): string {
    if (this._version === null) {
      this._version = this._computeVersion();
    }
    return this._version;
  }

  load(path: string): void {
    let content: string;
    try {
      content = readFileSync(path, "utf-8");
    } catch (err) {
      throw new Error(`Cannot read rates file ${path}: ${err instanceof Error ? err.message : err}`);
    }

    // eslint-disable-next-line @typescript-eslint/no-require-imports
    let yaml: { load: (s: string) => unknown };
    try {
      yaml = require("js-yaml") as typeof yaml;
    } catch {
      throw new Error("The 'js-yaml' package is required for YAML rate loading. Install it with: npm install js-yaml");
    }

    let parsed: unknown;
    try {
      parsed = yaml.load(content);
    } catch (err) {
      throw new Error(`Invalid YAML in rates file ${path}: ${err instanceof Error ? err.message : err}`);
    }

    const parsedObj = (parsed ?? {}) as Record<string, unknown>;
    const ratesData = parsedObj["rates"];
    if (typeof ratesData !== "object" || ratesData === null || Array.isArray(ratesData)) {
      throw new Error("Expected 'rates' key with a mapping in the YAML file.");
    }
    for (const [service, info] of Object.entries(ratesData)) {
      if (
        typeof info !== "object" ||
        info === null ||
        Array.isArray(info) ||
        !("cost_usd" in (info as object))
      ) {
        throw new Error(
          `Rate entry for ${JSON.stringify(service)} must be a mapping with at least 'cost_usd'.`
        );
      }
      const entry = info as Record<string, unknown>;
      const per = entry["per"] != null ? String(entry["per"]) : "unit";
      const costUsd = parseFloat(String(entry["cost_usd"]));
      this.register(String(service), per, costUsd);
    }
  }

  export(path: string): void {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    let yaml: { dump: (obj: unknown, opts?: Record<string, unknown>) => string };
    try {
      yaml = require("js-yaml") as typeof yaml;
    } catch {
      throw new Error("The 'js-yaml' package is required for YAML rate export. Install it with: npm install js-yaml");
    }

    const ratesData: Record<string, { per: string; cost_usd: string }> = {};
    for (const service of Array.from(this._rates.keys()).sort()) {
      const entry = this._rates.get(service)!;
      ratesData[service] = {
        per: entry.per,
        cost_usd: String(entry.costUsd),
      };
    }
    const output: string = yaml.dump({ rates: ratesData }, { sortKeys: false });

    try {
      writeFileSync(path, output, "utf-8");
    } catch (err) {
      throw new Error(`Cannot write rates file ${path}: ${err instanceof Error ? err.message : err}`);
    }
  }

  private _computeVersion(): string {
    const parts: string[] = [];
    for (const service of Array.from(this._rates.keys()).sort()) {
      const entry = this._rates.get(service)!;
      parts.push(`${service}:${entry.per}:${entry.costUsd}`);
    }
    const raw = parts.join("|");
    return createHash("sha256").update(raw, "utf-8").digest("hex").slice(0, 12);
  }
}
