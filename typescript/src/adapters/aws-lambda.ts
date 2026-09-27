/**
 * AWS Lambda cost adapter — compute cost from duration, memory, and region.
 *
 * `lambdaCost` is a pure function that returns the cost (in USD) and a
 * breakdown for a single Lambda invocation, using bundled pricing data
 * (no network I/O). Mirrors the Python SDK's `adapters/aws_lambda.py`.
 */

// Sprint 3 Theme E / §4.2.3 — Node 18 compat: runtime JSON load.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const _thisDir = dirname(fileURLToPath(import.meta.url));
const pricingData = JSON.parse(
  readFileSync(join(_thisDir, "data", "aws_lambda_pricing.json"), "utf-8"),
);

interface RegionPricing {
  duration_per_gb_second: Record<LambdaArchitecture, string>;
  request_per_invocation: string;
}

export type LambdaArchitecture = "x86_64" | "arm64";

interface LambdaPricingData {
  regions: Record<string, RegionPricing>;
}

const _pricing = pricingData as unknown as LambdaPricingData;

/** Breakdown of a Lambda invocation's cost. */
export interface LambdaCostDetails {
  region: string;
  architecture: LambdaArchitecture;
  durationMs: number;
  memoryMb: number;
  gbSeconds: number;
  durationCostUsd: number;
  requestCostUsd: number;
  ratePerGbSecond: number;
}

/** Result of {@link lambdaCost}. */
export interface LambdaCostResult {
  /** Total invocation cost in USD. */
  costUsd: number;
  /** Cost breakdown. */
  details: LambdaCostDetails;
}

/**
 * Return a sorted list of AWS region codes with bundled pricing data.
 */
export function getSupportedRegions(): string[] {
  return Object.keys(_pricing.regions).sort();
}

/**
 * Calculate the cost of a single AWS Lambda invocation.
 *
 * Pure function — no I/O, no side effects. Uses the bundled
 * `aws_lambda_pricing.json` for rates.
 *
 * @param durationMs - Execution duration in milliseconds (>= 0).
 * @param memoryMb - Allocated memory in MB (> 0).
 * @param region - AWS region code (e.g. `"us-east-1"`).
 * @param architecture - Lambda architecture. Defaults to `"x86_64"`.
 * @throws Error when an input is invalid or the region is unknown.
 */
export function lambdaCost(
  durationMs: number,
  memoryMb: number,
  region: string,
  architecture: LambdaArchitecture = "x86_64",
): LambdaCostResult {
  if (durationMs < 0) {
    throw new Error(`durationMs must be >= 0, got ${durationMs}`);
  }
  if (memoryMb <= 0) {
    throw new Error(`memoryMb must be > 0, got ${memoryMb}`);
  }
  if (architecture !== "x86_64" && architecture !== "arm64") {
    throw new Error(`Unsupported Lambda architecture '${String(architecture)}'`);
  }

  const regionPricing = _pricing.regions[region];
  if (regionPricing === undefined) {
    const supported = getSupportedRegions().join(", ");
    throw new Error(
      `Unknown AWS region '${region}'. Supported regions: ${supported}`,
    );
  }

  // GB-seconds = duration (s) * memory (GB)
  const durationSeconds = durationMs / 1000;
  const memoryGb = memoryMb / 1024;
  const gbSeconds = durationSeconds * memoryGb;

  const ratePerGbSecond = Number(regionPricing.duration_per_gb_second[architecture]);
  const requestCharge = Number(regionPricing.request_per_invocation);

  const durationCost = gbSeconds * ratePerGbSecond;
  const totalCost = durationCost + requestCharge;

  return {
    costUsd: totalCost,
    details: {
      region,
      architecture,
      durationMs,
      memoryMb,
      gbSeconds,
      durationCostUsd: durationCost,
      requestCostUsd: requestCharge,
      ratePerGbSecond,
    },
  };
}
