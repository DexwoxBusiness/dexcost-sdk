/**
 * Compute catalog integrity — structure, Decimal parsing, freshness,
 * dispatch coverage.
 *
 * Ports python/tests/test_compute_catalog_integrity.py (13 cases) to vitest.
 */

import { describe, expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import Decimal from "decimal.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const CATALOG_PATH = join(HERE, "../src/data/compute_prices.json");

function load(): Record<string, any> {
  return JSON.parse(readFileSync(CATALOG_PATH, "utf-8")) as Record<string, any>;
}

describe("compute catalog integrity", () => {
  test("catalog parses as JSON with _meta", () => {
    const data = load();
    expect(data).toHaveProperty("_meta");
  });

  test("_meta has required default keys", () => {
    const data = load();
    const meta = data._meta as Record<string, any>;
    const required = [
      "version",
      "last_updated",
      "currency",
      "default_lambda_request_usd",
      "default_lambda_gb_second_usd",
      "default_fargate_vcpu_second_usd",
      "default_fargate_gib_second_usd",
      "default_cloud_run_request_usd",
      "default_cloud_run_vcpu_second_usd",
      "default_cloud_run_gib_second_usd",
      "default_azure_functions_execution_usd",
      "default_azure_functions_gb_second_usd",
      "default_vercel_cpu_hour_usd",
      "default_vercel_memory_gb_hour_usd",
      "default_ec2_vcpu_hour_usd",
      "default_k8s_pod_vcpu_hour_usd",
      "description",
      "notes",
    ];
    for (const k of required) {
      expect(meta, `_meta missing ${k}`).toHaveProperty(k);
      if (k.startsWith("default_") && k.endsWith("_usd")) {
        // Must parse as Decimal.
        expect(() => new Decimal(meta[k])).not.toThrow();
      }
    }
    expect(meta.currency).toBe("USD");
  });

  test("every provider has _last_verified", () => {
    const data = load();
    const today = new Date();
    const softLimitDays = 180;
    for (const [provider, block] of Object.entries(data)) {
      if (provider === "_meta") continue;
      const verifiedRaw = (block as any)._last_verified;
      expect(verifiedRaw, `${provider} missing _last_verified`).toBeDefined();
      const verified = new Date(verifiedRaw);
      const days = Math.floor(
        (today.getTime() - verified.getTime()) / (24 * 3600 * 1000),
      );
      if (days > softLimitDays) {
        // Soft warn — do not fail.
        // eslint-disable-next-line no-console
        console.warn(
          `compute_prices.json: ${provider} _last_verified is ${days} days old (soft limit ${softLimitDays})`,
        );
      }
    }
  });

  test("all providers and runtimes present", () => {
    const data = load();
    for (const p of ["aws", "gcp", "azure", "vercel"]) {
      expect(data).toHaveProperty(p);
    }
    const awsRuntimes = new Set(Object.keys(data.aws).filter((k) => k !== "_last_verified"));
    for (const r of ["lambda", "fargate", "ec2"]) {
      expect(awsRuntimes.has(r), `aws missing runtime ${r}`).toBe(true);
    }
    const gcpRuntimes = new Set(Object.keys(data.gcp).filter((k) => k !== "_last_verified"));
    for (const r of ["cloud_run", "cloud_functions", "gce"]) {
      expect(gcpRuntimes.has(r), `gcp missing runtime ${r}`).toBe(true);
    }
    const azureRuntimes = new Set(Object.keys(data.azure).filter((k) => k !== "_last_verified"));
    for (const r of ["functions_consumption", "vm"]) {
      expect(azureRuntimes.has(r), `azure missing runtime ${r}`).toBe(true);
    }
    expect(data.vercel).toHaveProperty("fluid");
  });

  test("Lambda has both architectures", () => {
    const data = load();
    const def = data.aws.lambda.default;
    expect(new Set(Object.keys(def))).toEqual(new Set(["x86_64", "arm64"]));
    for (const arch of ["x86_64", "arm64"]) {
      expect(() => new Decimal(def[arch].request_usd)).not.toThrow();
      expect(() => new Decimal(def[arch].gb_second_usd)).not.toThrow();
    }
  });

  test("Fargate has both architectures", () => {
    const data = load();
    const def = data.aws.fargate.default;
    expect(new Set(Object.keys(def))).toEqual(new Set(["x86_64", "arm64"]));
    for (const arch of ["x86_64", "arm64"]) {
      expect(() => new Decimal(def[arch].vcpu_second_usd)).not.toThrow();
      expect(() => new Decimal(def[arch].gib_second_usd)).not.toThrow();
    }
  });

  test("ARM cheaper than x86 on Lambda", () => {
    const data = load();
    const firstRegion = Object.values<any>(data.aws.lambda.regions)[0];
    const arm = new Decimal(firstRegion.arm64.gb_second_usd);
    const x86 = new Decimal(firstRegion.x86_64.gb_second_usd);
    expect(arm.lt(x86), "arm64 must be cheaper than x86_64 on Lambda").toBe(true);
  });

  test("ARM cheaper than x86 on Fargate", () => {
    const data = load();
    const firstRegion = Object.values<any>(data.aws.fargate.regions)[0];
    const arm = new Decimal(firstRegion.arm64.vcpu_second_usd);
    const x86 = new Decimal(firstRegion.x86_64.vcpu_second_usd);
    expect(arm.lt(x86), "arm64 must be cheaper than x86_64 on Fargate").toBe(true);
  });

  test("top EC2 SKUs present for us-east-1", () => {
    const data = load();
    const its = data.aws.ec2.regions["us-east-1"].instance_types;
    for (const sku of ["c7g.xlarge", "m7i.large", "t3.medium"]) {
      expect(its, `missing EC2 SKU: ${sku}`).toHaveProperty(sku);
      expect(() => new Decimal(its[sku].hourly_usd)).not.toThrow();
      expect(() => new Decimal(its[sku].vcpu_count)).not.toThrow();
    }
  });

  test("top GCE SKUs present for us-central1", () => {
    const data = load();
    const its = data.gcp.gce.regions["us-central1"].instance_types;
    for (const sku of ["n2-standard-2", "e2-standard-4"]) {
      expect(its, `missing GCE SKU: ${sku}`).toHaveProperty(sku);
    }
  });

  test("top Azure VM SKUs present for eastus", () => {
    const data = load();
    const its = data.azure.vm.regions["eastus"].instance_types;
    for (const sku of ["Standard_D2s_v3", "Standard_B2ms"]) {
      expect(its, `missing Azure VM SKU: ${sku}`).toHaveProperty(sku);
    }
  });

  test("every rate is Decimal-parseable", () => {
    const data = load();

    function walk(node: unknown, path: string): void {
      if (node !== null && typeof node === "object" && !Array.isArray(node)) {
        for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
          walk(v, `${path}.${k}`);
        }
      } else if (typeof node === "string") {
        if (path.endsWith("_usd") || path.endsWith("vcpu_count")) {
          expect(() => new Decimal(node), `${path} not Decimal-parseable: ${node}`).not.toThrow();
        }
      }
    }
    walk(data, "");
  });

  test("every dispatch billing_model has a rate path", () => {
    const data = load();
    const meta = data._meta;
    for (const k of [
      "default_lambda_request_usd",
      "default_fargate_vcpu_second_usd",
      "default_cloud_run_request_usd",
      "default_azure_functions_execution_usd",
      "default_vercel_cpu_hour_usd",
      "default_ec2_vcpu_hour_usd",
      "default_k8s_pod_vcpu_hour_usd",
    ]) {
      expect(meta).toHaveProperty(k);
    }
  });
});
