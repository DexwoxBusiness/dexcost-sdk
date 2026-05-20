/**
 * Tests for the network-adapter helper functions.
 *
 * Mirrors python/tests/test_netbytes.py.
 */

import { describe, it, expect } from "vitest";
import {
  classifyDestination,
  measureBytesFromHeaders,
} from "../src/adapters/_netbytes.js";

describe("classifyDestination", () => {
  it("classifies private IPv4 as internal", () => {
    expect(classifyDestination("10.1.2.3")).toBe(true);
    expect(classifyDestination("192.168.0.5")).toBe(true);
    expect(classifyDestination("172.16.9.9")).toBe(true);
    expect(classifyDestination("172.31.255.255")).toBe(true);
  });

  it("classifies localhost and link-local as internal", () => {
    expect(classifyDestination("127.0.0.1")).toBe(true);
    expect(classifyDestination("::1")).toBe(true);
    expect(classifyDestination("169.254.10.1")).toBe(true);
  });

  it("classifies public IPv4 as external", () => {
    expect(classifyDestination("8.8.8.8")).toBe(false);
    expect(classifyDestination("1.1.1.1")).toBe(false);
    expect(classifyDestination("172.32.0.1")).toBe(false); // just outside 172.16/12
    expect(classifyDestination("172.15.0.1")).toBe(false); // just below 172.16/12
  });

  it("returns null for hostnames and empty input", () => {
    expect(classifyDestination("api.openai.com")).toBeNull();
    expect(classifyDestination("")).toBeNull();
    expect(classifyDestination("not.an.ip")).toBeNull();
    expect(classifyDestination("999.999.999.999")).toBeNull();
  });

  it("classifies IPv6 ULA (fc00::/7) as internal", () => {
    expect(classifyDestination("fd00::1")).toBe(true);
    expect(classifyDestination("fc00::1")).toBe(true);
    expect(classifyDestination("fdab:cdef::1")).toBe(true);
  });

  it("classifies IPv6 link-local (fe80::/10) as internal", () => {
    expect(classifyDestination("fe80::1")).toBe(true);
    expect(classifyDestination("febf::1")).toBe(true);
  });

  it("classifies public IPv6 as external", () => {
    expect(classifyDestination("2001:4860:4860::8888")).toBe(false);
    expect(classifyDestination("2606:4700:4700::1111")).toBe(false);
  });

  it("CGNAT shared range 100.64.0.0/10 is NOT classified as internal", () => {
    // Mirrors the Python comment: ipaddress.is_private does not include
    // 100.64.0.0/10 (RFC 6598), so it returns False.
    expect(classifyDestination("100.64.0.1")).toBe(false);
    expect(classifyDestination("100.127.255.255")).toBe(false);
  });
});

describe("measureBytesFromHeaders", () => {
  it("includes headers and body", () => {
    const headers = {
      "Content-Length": "2048",
      "Content-Type": "application/json",
    };
    const n = measureBytesFromHeaders(
      "POST",
      "https://x.com/v1/y",
      headers,
      2048,
    );
    expect(n).toBeGreaterThanOrEqual(2048);
    expect(n).toBeGreaterThan(2048);
  });

  it("computes the exact total for the canonical fixture", () => {
    // Mirror Python's exact-total test:
    // request_line = len("GET") + len("https://a.io/") + 12 = 3 + 13 + 12 = 28
    // headers:      (len("X-H") + len("v") + 4) + 2          = 10
    // body = 0
    // total = 38
    const n = measureBytesFromHeaders("GET", "https://a.io/", { "X-H": "v" }, 0);
    expect(n).toBe(38);
  });

  it("returns a positive number for empty headers + zero body", () => {
    const n = measureBytesFromHeaders("GET", "https://x.com/", {}, 0);
    expect(n).toBeGreaterThan(0);
  });

  it("treats negative body_len as zero", () => {
    const n = measureBytesFromHeaders("GET", "/", {}, -100);
    // request_line = len("GET") + len("/") + 12 = 3 + 1 + 12 = 16
    // headers trailing = 2; body clamps to 0
    expect(n).toBe(18);
  });
});
