/**
 * Tests for the AWS Lambda cost adapter.
 */

import { describe, it, expect } from "vitest";
import { lambdaCost, getSupportedRegions } from "../src/adapters/aws-lambda.js";

describe("getSupportedRegions", () => {
  it("returns a sorted, non-empty list of region codes", () => {
    const regions = getSupportedRegions();
    expect(regions.length).toBeGreaterThan(0);
    expect(regions).toContain("us-east-1");
    expect([...regions].sort()).toEqual(regions);
  });
});

describe("lambdaCost", () => {
  it("computes cost from duration, memory, and region", () => {
    // 1000 ms at 1024 MB in us-east-1.
    const result = lambdaCost(1000, 1024, "us-east-1");
    // gb_seconds = 1s * 1GB = 1; duration cost = 1 * 0.0000166667
    // request charge = 0.0000002
    expect(result.costUsd).toBeCloseTo(0.0000166667 + 0.0000002, 12);
    expect(result.details.region).toBe("us-east-1");
    expect(result.details.gbSeconds).toBeCloseTo(1, 9);
    expect(result.details.durationMs).toBe(1000);
    expect(result.details.memoryMb).toBe(1024);
  });

  it("returns zero duration cost for zero duration", () => {
    const result = lambdaCost(0, 512, "us-east-1");
    expect(result.details.durationCostUsd).toBe(0);
    // Only the per-request charge remains.
    expect(result.costUsd).toBeCloseTo(0.0000002, 12);
  });

  it("throws for a negative duration", () => {
    expect(() => lambdaCost(-1, 512, "us-east-1")).toThrow(/durationMs/);
  });

  it("throws for non-positive memory", () => {
    expect(() => lambdaCost(100, 0, "us-east-1")).toThrow(/memoryMb/);
  });

  it("throws for an unknown region", () => {
    expect(() => lambdaCost(100, 512, "mars-1")).toThrow(/Unknown AWS region/);
  });
});
