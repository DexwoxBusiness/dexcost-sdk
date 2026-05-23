/**
 * cgroup v2 file parsing — cpu.stat / cpu.max / memory.peak / memory.max /
 * memory.current.
 *
 * Ports python/tests/test_cgroup_reader.py (12 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  readCpuStat,
  readCpuMax,
  readMemoryPeak,
  readMemoryMax,
  readMemoryCurrent,
  _setCgroupRootForTests,
} from "../src/core/cgroup-reader.js";

let tmp: string;

function seed(files: Record<string, string>): void {
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(join(tmp, name), body);
  }
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "dexcost-cgroup-"));
  _setCgroupRootForTests(tmp);
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
  _setCgroupRootForTests(null);
});

describe("cgroup reader", () => {
  test("read_cpu_stat parses usage_usec", () => {
    seed({
      "cpu.stat":
        "usage_usec 12345\n" +
        "user_usec 6000\n" +
        "system_usec 6345\n" +
        "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n",
    });
    const s = readCpuStat();
    expect(s).not.toBeNull();
    expect(s!.usageUsec).toBe(12345);
  });

  test("read_cpu_max with quota", () => {
    seed({ "cpu.max": "100000 100000\n" });
    const m = readCpuMax();
    expect(m).toEqual({ quotaUs: 100000, periodUs: 100000, vcpuCount: 1.0 });
  });

  test("read_cpu_max quota fraction", () => {
    // 25000 / 100000 = 0.25 vCPU (a small Fargate task).
    seed({ "cpu.max": "25000 100000\n" });
    const m = readCpuMax();
    expect(m).not.toBeNull();
    expect(m!.vcpuCount).toBe(0.25);
  });

  test("read_cpu_max unlimited falls back to nproc", () => {
    seed({ "cpu.max": "max 100000\n" });
    const m = readCpuMax();
    expect(m).not.toBeNull();
    expect(m!.quotaUs).toBeNull();
    expect(m!.vcpuCount).toBeGreaterThan(0);
  });

  test("read_memory_peak", () => {
    seed({ "memory.peak": "2147483648\n" });
    expect(readMemoryPeak()).toBe(2147483648);
  });

  test("read_memory_max finite", () => {
    seed({ "memory.max": "1073741824\n" });
    expect(readMemoryMax()).toBe(1073741824);
  });

  test("read_memory_max unlimited returns null", () => {
    seed({ "memory.max": "max\n" });
    expect(readMemoryMax()).toBeNull();
  });

  test("read_memory_current", () => {
    seed({ "memory.current": "1024\n" });
    expect(readMemoryCurrent()).toBe(1024);
  });

  test("missing files return null", () => {
    expect(readCpuStat()).toBeNull();
    expect(readCpuMax()).toBeNull();
    expect(readMemoryPeak()).toBeNull();
    expect(readMemoryMax()).toBeNull();
    expect(readMemoryCurrent()).toBeNull();
  });

  test("malformed cpu.stat returns null", () => {
    seed({ "cpu.stat": "garbage\n" });
    expect(readCpuStat()).toBeNull();
  });

  test("malformed cpu.max returns null", () => {
    seed({ "cpu.max": "only-one-token\n" });
    expect(readCpuMax()).toBeNull();
  });

  test("memory.peak absent when kernel too old, current still present", () => {
    seed({ "memory.current": "1024\n" });
    expect(readMemoryPeak()).toBeNull();
    expect(readMemoryCurrent()).toBe(1024);
  });
});
