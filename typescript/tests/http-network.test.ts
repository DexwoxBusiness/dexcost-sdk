/**
 * HTTP adapter network-capture tests (Task 7 — Phase B).
 *
 * Mirrors python/tests/test_network_capture.py and the Rust + Go
 * adapter tests.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  trackHttp,
  untrackHttp,
  registerDomainRate,
  clearDomainRates,
  getRecordedEvents,
  clearRecordedEvents,
} from "../src/adapters/http.js";
import {
  NetworkAccountant,
  registerAccountant,
  _resetAccountantRegistryForTests,
} from "../src/adapters/network-accountant.js";
import { suppressNetworkEvent } from "../src/core/context.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";

// Spin up a tiny in-process server for each test using Node's built-in
// http module. Avoids external deps.
import http from "node:http";

function startServer(handler: http.RequestListener): Promise<{ url: string; close: () => Promise<void> }> {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address() as { port: number };
      const url = `http://127.0.0.1:${addr.port}`;
      const close = () =>
        new Promise<void>((res) => {
          server.close(() => res());
        });
      resolve({ url, close });
    });
  });
}

describe("HTTP adapter — Task 7 byte accounting + network events", () => {
  beforeEach(() => {
    clearDomainRates();
    clearRecordedEvents();
    _resetAccountantRegistryForTests();
    trackHttp();
  });

  afterEach(() => {
    untrackHttp();
    clearDomainRates();
    clearRecordedEvents();
    _resetAccountantRegistryForTests();
  });

  it("records bytes into the registered accountant", async () => {
    const server = await startServer((req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("hello world");
    });

    const task = createTask({ taskId: "t-bytes" });
    const accountant = new NetworkAccountant();
    registerAccountant(task.taskId, accountant);

    await runWithTask(task, async () => {
      const r = await fetch(server.url + "/x");
      const body = await r.text();
      expect(body).toBe("hello world");
    });

    await server.close();
    const snap = accountant.finalize();
    expect(snap.callCount).toBe(1);
    expect(snap.bytesIn).toBeGreaterThan(0);
    expect(snap.bytesOut).toBeGreaterThan(0);
  });

  it("un-cataloged above-threshold call emits a network event with cost_pending", async () => {
    // 200 KB body — exceeds the 100 KiB threshold.
    const big = "x".repeat(200_000);
    const server = await startServer((req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end(big);
    });

    const task = createTask({ taskId: "t-big" });
    registerAccountant(task.taskId, new NetworkAccountant());

    await runWithTask(task, async () => {
      const r = await fetch(server.url + "/big");
      await r.text(); // drain → TransformStream flush → finalise
    });

    await server.close();
    const events = getRecordedEvents();
    const netEvents = events.filter((e) => e.eventType === "network");
    expect(netEvents.length).toBe(1);
    expect(netEvents[0].costUsd).toBe(0);
    expect(netEvents[0].details?.cost_pending).toBe(true);
    expect(typeof netEvents[0].details?.request_bytes).toBe("number");
    expect(typeof netEvents[0].details?.response_bytes).toBe("number");
    expect(netEvents[0].details?.is_internal_traffic).toBeDefined();
  });

  it("un-cataloged below-threshold call emits no network event (counters only)", async () => {
    const server = await startServer((req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("tiny");
    });

    const task = createTask({ taskId: "t-small" });
    registerAccountant(task.taskId, new NetworkAccountant());

    await runWithTask(task, async () => {
      const r = await fetch(server.url + "/small");
      await r.text();
    });

    await server.close();
    const events = getRecordedEvents();
    const netEvents = events.filter((e) => e.eventType === "network");
    expect(netEvents.length).toBe(0);
    // The placeholder external_cost-zero event is also dropped on the
    // counters-only path (matches Python v1 §4.4 noise-removal).
    const externalZero = events.filter(
      (e) => e.eventType === "external_cost" && e.costUsd === 0,
    );
    expect(externalZero.length).toBe(0);
  });

  it("suppression scope withholds the network event even above threshold", async () => {
    const big = "x".repeat(200_000);
    const server = await startServer((req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end(big);
    });

    const task = createTask({ taskId: "t-suppressed" });
    const accountant = new NetworkAccountant();
    registerAccountant(task.taskId, accountant);

    await runWithTask(task, async () => {
      await suppressNetworkEvent(async () => {
        const r = await fetch(server.url + "/big");
        await r.text();
      });
    });

    await server.close();
    const events = getRecordedEvents();
    const netEvents = events.filter((e) => e.eventType === "network");
    expect(netEvents.length).toBe(0);
    // Bytes still recorded into the accountant.
    const snap = accountant.finalize();
    expect(snap.callCount).toBe(1);
    expect(snap.bytesIn).toBeGreaterThan(0);
  });

  it("domain-rate call emits external_cost with byte_details (request side)", async () => {
    const server = await startServer((req, res) => {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("ok");
    });

    // Register the rate against the actual local hostname (127.0.0.1).
    registerDomainRate("127.0.0.1", 0.01, "request");

    const task = createTask({ taskId: "t-rate" });
    registerAccountant(task.taskId, new NetworkAccountant());

    await runWithTask(task, async () => {
      const r = await fetch(server.url + "/x");
      await r.text();
    });

    await server.close();
    const events = getRecordedEvents();
    const ext = events.filter((e) => e.eventType === "external_cost");
    expect(ext.length).toBeGreaterThan(0);
    const ev = ext[ext.length - 1];
    expect(ev.costUsd).toBe(0.01);
    expect(ev.details?.protocol).toBe("http");
    expect(typeof ev.details?.request_bytes).toBe("number");
    expect(ev.details?.is_internal_traffic).toBe(true); // 127.0.0.1 → loopback
  });
});
