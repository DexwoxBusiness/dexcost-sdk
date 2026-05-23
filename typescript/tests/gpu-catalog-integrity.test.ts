/**
 * GPU catalog integrity — structure, Decimal parsing, freshness, SKU consistency.
 *
 * Ports python/tests/test_gpu_catalog_integrity.py to vitest. Pins the
 * STRUCTURAL invariants so a future refresh can't drift shape; freshness
 * check enforces Decision #11's tighter 90/365-day thresholds.
 */

import { describe, expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import Decimal from "decimal.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const CATALOG_PATH = join(HERE, "../src/data/gpu_prices.json");

function load(): Record<string, any> {
  return JSON.parse(readFileSync(CATALOG_PATH, "utf-8")) as Record<string, any>;
}

function isoDate(s: string): Date {
  // Strict ISO-8601 YYYY-MM-DD parser — throws on malformed input.
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    throw new Error(`not ISO-8601 date: ${s}`);
  }
  const d = new Date(`${s}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) throw new Error(`not valid date: ${s}`);
  return d;
}

function daysBetween(a: Date, b: Date): number {
  return Math.round((a.getTime() - b.getTime()) / (1000 * 60 * 60 * 24));
}

describe("GPU catalog integrity", () => {
  test("catalog parses as JSON with _meta", () => {
    const data = load();
    expect(data).toHaveProperty("_meta");
  });

  test("_meta has required default keys (all 4 billing models)", () => {
    const data = load();
    const meta = data._meta as Record<string, any>;
    const required = [
      "version",
      "last_updated",
      "currency",
      "default_per_instance_hour_usd",
      "default_per_gpu_second_active_usd",
      "default_per_gpu_hour_reserved_usd",
      "default_per_vgpu_hour_usd",
      "description",
      "notes",
    ];
    for (const k of required) {
      expect(meta).toHaveProperty(k);
      if (k.startsWith("default_") && k.endsWith("_usd")) {
        // Decimal-parseable.
        new Decimal(String(meta[k]));
      }
    }
    expect(meta.currency).toBe("USD");
  });

  test("all 8 providers present", () => {
    const data = load();
    const expected = new Set([
      "aws",
      "gcp",
      "azure",
      "modal",
      "runpod",
      "lambda_labs",
      "coreweave",
      "replicate",
    ]);
    for (const e of expected) {
      expect(data).toHaveProperty(e);
    }
  });

  test("every provider has _last_verified as ISO-8601 date", () => {
    const data = load();
    for (const [k, block] of Object.entries(data)) {
      if (k === "_meta") continue;
      const lv = (block as Record<string, any>)._last_verified as string;
      isoDate(lv); // throws on bad
    }
  });

  test("Decision #11 soft-warn at 90 days (deterministic simulation)", () => {
    const data = load();
    const dates = Object.entries(data)
      .filter(([k]) => k !== "_meta")
      .map(([, b]) => isoDate((b as Record<string, any>)._last_verified));
    const earliest = dates.reduce(
      (acc, d) => (d < acc ? d : acc),
      dates[0],
    );
    const fakeToday = new Date(
      earliest.getTime() + 91 * 24 * 60 * 60 * 1000,
    );
    const warnings: string[] = [];
    for (const [provider, block] of Object.entries(data)) {
      if (provider === "_meta") continue;
      const verified = isoDate(
        (block as Record<string, any>)._last_verified,
      );
      if (daysBetween(fakeToday, verified) > 90) {
        warnings.push(
          `gpu_prices.json: ${provider} _last_verified is ${daysBetween(fakeToday, verified)} days old (soft limit 90)`,
        );
      }
    }
    expect(warnings.some((w) => w.includes("soft limit 90"))).toBe(true);
  });

  test("Decision #11 hard-fail at 365 days", () => {
    const data = load();
    const today = new Date();
    const stale: string[] = [];
    for (const [provider, block] of Object.entries(data)) {
      if (provider === "_meta") continue;
      const verified = isoDate(
        (block as Record<string, any>)._last_verified,
      );
      const days = daysBetween(today, verified);
      if (days > 365) stale.push(`${provider}: ${days}d`);
    }
    expect(stale).toEqual([]);
  });

  test("AWS has ec2_gpu.regions.us-east-1", () => {
    const data = load();
    expect(data.aws).toHaveProperty("ec2_gpu");
    expect(data.aws.ec2_gpu).toHaveProperty("regions");
    expect(data.aws.ec2_gpu.regions).toHaveProperty("us-east-1");
  });

  test("GCP has both attached and bundled blocks", () => {
    const data = load();
    expect(data.gcp).toHaveProperty("gce_gpu_attached");
    expect(data.gcp).toHaveProperty("gce_gpu_bundled");
  });

  test("Azure has both vm_gpu and vm_vgpu blocks", () => {
    const data = load();
    expect(data.azure).toHaveProperty("vm_gpu");
    expect(data.azure).toHaveProperty("vm_vgpu");
  });

  test("serverless providers have per_gpu_second_active", () => {
    const data = load();
    for (const p of ["modal", "runpod", "replicate"]) {
      expect(data[p]).toHaveProperty("per_gpu_second_active");
    }
  });

  test("reserved providers have per_gpu_hour_reserved", () => {
    const data = load();
    for (const p of ["lambda_labs", "coreweave"]) {
      expect(data[p]).toHaveProperty("per_gpu_hour_reserved");
    }
  });

  test("every USD field is Decimal-parseable", () => {
    const data = load();
    function walk(node: unknown, path: string): void {
      if (node && typeof node === "object" && !Array.isArray(node)) {
        for (const [k, v] of Object.entries(node)) {
          if (
            typeof v === "string" &&
            (k.endsWith("_usd") ||
              k === "vcpu_count" ||
              k === "gpu_count" ||
              k === "gpu_vram_gb" ||
              k === "memory_gb")
          ) {
            try {
              new Decimal(v);
            } catch (exc) {
              throw new Error(
                `${path}.${k} not Decimal-parseable: ${v} (${String(exc)})`,
              );
            }
          } else {
            walk(v, `${path}.${k}`);
          }
        }
      } else if (Array.isArray(node)) {
        node.forEach((item, i) => walk(item, `${path}[${i}]`));
      }
    }
    walk(data, "");
  });

  test("h100-80gb-sxm5 SKU consistent across all 8 providers", () => {
    const data = load();
    const found = new Set<string>();
    function walk(node: unknown, provider: string): void {
      if (node && typeof node === "object" && !Array.isArray(node)) {
        const obj = node as Record<string, unknown>;
        if (obj.gpu_sku === "h100-80gb-sxm5") found.add(provider);
        for (const v of Object.values(obj)) walk(v, provider);
      } else if (Array.isArray(node)) {
        for (const v of node) walk(v, provider);
      }
    }
    for (const provider of Object.keys(data)) {
      if (provider === "_meta") continue;
      walk(data[provider], provider);
    }
    const expected = new Set([
      "aws",
      "gcp",
      "azure",
      "modal",
      "runpod",
      "lambda_labs",
      "coreweave",
      "replicate",
    ]);
    expect(found).toEqual(expected);
  });

  test("every dispatch billing model has a meta default", () => {
    const meta = load()._meta as Record<string, any>;
    expect(meta).toHaveProperty("default_per_instance_hour_usd");
    expect(meta).toHaveProperty("default_per_gpu_second_active_usd");
    expect(meta).toHaveProperty("default_per_gpu_hour_reserved_usd");
    expect(meta).toHaveProperty("default_per_vgpu_hour_usd");
  });

  test("sample SKU entries carry aliases arrays", () => {
    const data = load();
    const samples = [
      data.aws.ec2_gpu.regions["us-east-1"].instance_types["p5.48xlarge"],
      data.modal.per_gpu_second_active.default.h100,
      data.azure.vm_vgpu.regions.eastus.instance_types[
        "Standard_NV6ads_A10_v5"
      ],
    ];
    for (const entry of samples) {
      expect(entry).toHaveProperty("aliases");
      expect(Array.isArray(entry.aliases)).toBe(true);
    }
  });

  test("Decision #5 — GPU catalog has no arch nesting (gpu_sku flat)", () => {
    const data = load();
    const p5 =
      data.aws.ec2_gpu.regions["us-east-1"].instance_types["p5.48xlarge"];
    expect(p5).toHaveProperty("gpu_sku");
    expect(p5).not.toHaveProperty("x86_64");
    expect(p5).not.toHaveProperty("arm64");
  });

  test("_meta.version is semver-like", () => {
    const meta = load()._meta as Record<string, any>;
    const v = String(meta.version);
    const parts = v.split(".");
    expect(parts.length).toBe(3);
    for (const p of parts) {
      expect(Number.isInteger(parseInt(p, 10))).toBe(true);
    }
  });
});
