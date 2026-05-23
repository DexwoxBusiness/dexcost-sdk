/**
 * NetworkAccountant tests — port of python/tests/test_network_accountant.py
 * and test_network_accountant_external.py.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  NetworkAccountant,
  FINALIZE_CAP,
  LIVE_CAP,
  registerAccountant,
  getAccountant,
  unregisterAccountant,
  _resetAccountantRegistryForTests,
} from "../src/adapters/network-accountant.js";

describe("NetworkAccountant — record + finalize", () => {
  it("record updates counters", () => {
    const a = new NetworkAccountant();
    a.record("a.com", 100, 10);
    a.record("a.com", 50, 5);
    const snap = a.finalize();
    expect(snap.bytesIn).toBe(150);
    expect(snap.bytesOut).toBe(15);
    expect(snap.callCount).toBe(2);
  });

  it("finalize groups by host", () => {
    const a = new NetworkAccountant();
    a.record("a.com", 100, 10);
    a.record("b.com", 200, 20);
    const snap = a.finalize();
    const a_host = snap.byHost.hosts.find((h) => h.host === "a.com")!;
    expect(a_host.calls).toBe(1);
    expect(a_host.bytes_in).toBe(100);
    expect(a_host.bytes_out).toBe(10);
    // isInternal default null → bytes_out attributes as external.
    expect(a_host.external_bytes_out).toBe(10);
  });

  it("finalize caps to top 20 with _other bucket", () => {
    const a = new NetworkAccountant();
    for (let i = 0; i < 25; i++) {
      a.record(`h${String(i).padStart(2, "0")}.com`, i + 1, 0);
    }
    const hosts = a.finalize().byHost.hosts;
    expect(hosts.length).toBe(FINALIZE_CAP + 1);
    const names = new Set(hosts.map((h) => h.host));
    expect(names.has("_other")).toBe(true);
    expect(names.has("h24.com")).toBe(true);
    expect(names.has("h00.com")).toBe(false);
    const other = hosts.find((h) => h.host === "_other")!;
    expect(other.calls).toBe(5);
    expect(other.bytes_in).toBe(1 + 2 + 3 + 4 + 5);
  });

  it("empty finalize returns empty hosts array", () => {
    expect(new NetworkAccountant().finalize().byHost).toEqual({ hosts: [] });
  });

  it("LIVE_CAP overflow folds into _other", () => {
    const a = new NetworkAccountant();
    for (let i = 0; i < LIVE_CAP + 50; i++) {
      a.record(`host${i}.com`, 0, 1, false);
    }
    expect(a.liveHostCount()).toBe(LIVE_CAP);
    const hosts = a.finalize().byHost.hosts;
    const other = hosts.find((h) => h.host === "_other")!;
    expect(other.calls).toBe(LIVE_CAP + 50 - FINALIZE_CAP);
  });

  it("frozen after finalize — subsequent record is no-op", () => {
    const a = new NetworkAccountant();
    a.record("a.com", 100, 10);
    const snap1 = a.finalize();
    a.record("b.com", 999, 999);
    const snap2 = a.finalize();
    expect(snap1.bytesIn).toBe(snap2.bytesIn);
    expect(snap1.callCount).toBe(snap2.callCount);
  });

  it("empty host falls back to _unknown", () => {
    const a = new NetworkAccountant();
    a.record("", 10, 0);
    const hosts = a.finalize().byHost.hosts;
    expect(hosts[0].host).toBe("_unknown");
  });

  it("negative bytes clamped to zero", () => {
    const a = new NetworkAccountant();
    a.record("a.com", -10, -20);
    const snap = a.finalize();
    expect(snap.bytesIn).toBe(0);
    expect(snap.bytesOut).toBe(0);
  });

  it("synthetic _other collides with real host literally named '_other'", () => {
    const a = new NetworkAccountant();
    a.record("_other", 100, 50);
    a.record("real.com", 1, 1);
    const hosts = a.finalize().byHost.hosts;
    const otherCount = hosts.filter((h) => h.host === "_other").length;
    expect(otherCount).toBe(1);
    const other = hosts.find((h) => h.host === "_other")!;
    expect(other.bytes_in).toBe(100);
  });
});

describe("NetworkAccountant — external-byte split (v2)", () => {
  it("internal call does not contribute to external", () => {
    const a = new NetworkAccountant();
    a.record("10.0.0.5", 100, 200, true);
    const snap = a.finalize();
    expect(snap.externalBytesOut).toBe(0);
    const host = snap.byHost.hosts[0];
    expect(host.external_bytes_out).toBe(0);
    expect(host.bytes_out).toBe(200);
  });

  it("public call contributes to external", () => {
    const a = new NetworkAccountant();
    a.record("api.example.com", 100, 500, false);
    expect(a.finalize().externalBytesOut).toBe(500);
  });

  it("null isInternal treated as external (conservative)", () => {
    const a = new NetworkAccountant();
    a.record("api.example.com", 100, 500, null);
    expect(a.finalize().externalBytesOut).toBe(500);
  });

  // v2 §10.3 invariant 1: scalar external == sum of per-host external.
  it("scalar external equals sum of per-host external", () => {
    const a = new NetworkAccountant();
    a.record("a.com", 0, 100, false);
    a.record("b.com", 0, 200, false);
    a.record("10.0.0.1", 0, 999, true);
    const snap = a.finalize();
    const sum = snap.byHost.hosts.reduce(
      (acc, h) => acc + (h.external_bytes_out as number),
      0,
    );
    expect(sum).toBe(snap.externalBytesOut);
    expect(snap.externalBytesOut).toBe(300);
  });

  it("_other bucket carries external bytes through LIVE_CAP + top-20 folds", () => {
    const a = new NetworkAccountant();
    for (let i = 0; i < LIVE_CAP; i++) {
      a.record(`host${i}.com`, 0, 1, false);
    }
    a.record("overflow.com", 0, 555, false);
    const hosts = a.finalize().byHost.hosts;
    const other = hosts.find((h) => h.host === "_other")!;
    expect(other.external_bytes_out).toBe(LIVE_CAP - FINALIZE_CAP + 555);
  });

  it("default null isInternal routes bytes as external", () => {
    const a = new NetworkAccountant();
    a.record("api.example.com", 0, 100);
    expect(a.finalize().externalBytesOut).toBe(100);
  });
});

describe("NetworkAccountant — registry", () => {
  beforeEach(() => {
    _resetAccountantRegistryForTests();
  });

  it("register then get returns same instance", () => {
    const a = new NetworkAccountant();
    registerAccountant("t-1", a);
    expect(getAccountant("t-1")).toBe(a);
  });

  it("get missing returns undefined", () => {
    expect(getAccountant("does-not-exist")).toBeUndefined();
  });

  it("unregister returns then removes; idempotent", () => {
    const a = new NetworkAccountant();
    registerAccountant("t-1", a);
    expect(unregisterAccountant("t-1")).toBe(a);
    expect(getAccountant("t-1")).toBeUndefined();
    expect(unregisterAccountant("t-1")).toBeUndefined();
  });
});
