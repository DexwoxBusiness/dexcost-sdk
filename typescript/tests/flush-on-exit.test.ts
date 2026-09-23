/**
 * B9 regression — Sprint 2 Theme E / plan §3.3.2.
 *
 * If init() doesn't install beforeExit/SIGTERM/SIGINT handlers, events
 * recorded just before `process.exit(0)` are lost — the buffered
 * in-memory queue + the not-yet-flushed pusher batch both die with
 * the process. The plan asks for handlers that synchronously flush
 * the buffer (already-on-disk via SQLite) and await the in-flight
 * push with a short timeout.
 *
 * Spawning a real child process to drive process.exit is the
 * canonical test, but it requires CLI/scripting infrastructure. We
 * exercise the contract at a finer grain: assert init() registers
 * the listeners and a SIGTERM emit triggers closeAsync.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { init, close } from "../src/index.js";
import { EventBuffer } from "../src/transport/buffer.js";

describe("flush on exit (B9)", () => {
  const installedListeners: Array<{ event: string; listener: NodeJS.SignalsListener }> = [];

  afterEach(() => {
    for (const { event, listener } of installedListeners) {
      process.off(event, listener as never);
    }
    installedListeners.length = 0;
    try {
      close();
    } catch {
      // already closed
    }
    EventBuffer._forceFallbackForTest = false;
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("init registers beforeExit + SIGTERM + SIGINT listeners", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});

    const before = {
      beforeExit: process.listenerCount("beforeExit"),
      SIGTERM: process.listenerCount("SIGTERM"),
      SIGINT: process.listenerCount("SIGINT"),
    };

    init({ apiKey: "dx_test_x" });

    const after = {
      beforeExit: process.listenerCount("beforeExit"),
      SIGTERM: process.listenerCount("SIGTERM"),
      SIGINT: process.listenerCount("SIGINT"),
    };

    expect(after.beforeExit).toBe(before.beforeExit + 1);
    expect(after.SIGTERM).toBe(before.SIGTERM + 1);
    expect(after.SIGINT).toBe(before.SIGINT + 1);
  });

  test("close() removes the exit listeners (no leak across init/close cycles)", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});

    const baseline = {
      beforeExit: process.listenerCount("beforeExit"),
      SIGTERM: process.listenerCount("SIGTERM"),
      SIGINT: process.listenerCount("SIGINT"),
    };

    init({ apiKey: "dx_test_x" });
    close();

    const after = {
      beforeExit: process.listenerCount("beforeExit"),
      SIGTERM: process.listenerCount("SIGTERM"),
      SIGINT: process.listenerCount("SIGINT"),
    };

    expect(after).toEqual(baseline);
  });

  test("sole SDK SIGTERM listener flushes once then restores default termination", async () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const before = new Set(process.listeners("SIGTERM"));
    const tracker = init({ apiKey: "dx_test_x" });
    const sdkListener = process.listeners("SIGTERM").find((listener) => !before.has(listener));
    expect(sdkListener).toBeDefined();
    const closeAsync = vi.spyOn(tracker, "closeAsync").mockResolvedValue();
    const originalListeners = process.listeners.bind(process);
    vi.spyOn(process, "listeners").mockImplementation(((event: string | symbol) => (
      event === "SIGTERM" ? [sdkListener as NodeJS.SignalsListener] : originalListeners(event)
    )) as typeof process.listeners);
    const kill = vi.spyOn(process, "kill").mockImplementation((() => true) as typeof process.kill);

    (sdkListener as NodeJS.SignalsListener)("SIGTERM");
    (sdkListener as NodeJS.SignalsListener)("SIGTERM");

    await vi.waitFor(() => expect(kill).toHaveBeenCalledOnce());
    expect(closeAsync).toHaveBeenCalledOnce();
    expect(kill).toHaveBeenCalledWith(process.pid, "SIGTERM");
  });

  test("host SIGTERM listener retains lifecycle ownership", async () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const before = new Set(process.listeners("SIGTERM"));
    init({ apiKey: "dx_test_x" });
    const sdkListener = process.listeners("SIGTERM").find((listener) => !before.has(listener));
    expect(sdkListener).toBeDefined();
    const hostListener: NodeJS.SignalsListener = () => {};
    const originalListeners = process.listeners.bind(process);
    vi.spyOn(process, "listeners").mockImplementation(((event: string | symbol) => (
      event === "SIGTERM"
        ? [hostListener, sdkListener as NodeJS.SignalsListener]
        : originalListeners(event)
    )) as typeof process.listeners);
    const kill = vi.spyOn(process, "kill").mockImplementation((() => true) as typeof process.kill);

    (sdkListener as NodeJS.SignalsListener)("SIGTERM");

    await vi.waitFor(() => expect(process.listenerCount("SIGTERM")).toBe(before.size));
    expect(kill).not.toHaveBeenCalled();
  });

  test("beforeExit timeout aborts a stalled ingest request", async () => {
    vi.useFakeTimers();
    EventBuffer._forceFallbackForTest = true;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const before = new Set(process.listeners("beforeExit"));
    const endpoint = "https://flush-request-test.invalid";
    const tracker = init({
      apiKey: "dx_test_x",
      autoInstrument: [],
      trackHttp: false,
      endpoint,
    });
    const sdkListener = process.listeners("beforeExit").find((listener) => !before.has(listener));
    expect(sdkListener).toBeDefined();

    await tracker.track({ taskType: "stalled-shutdown" }, async (task) => {
      task.recordCost("test", 0.001);
    });

    let aborted = false;
    const fetchMock = vi.fn((url: string | URL | Request, init?: RequestInit) => {
      if (String(url) !== `${endpoint}/v1/ingest`) {
        return Promise.resolve({ ok: true, status: 202, json: async () => ({}) } as Response);
      }
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          aborted = true;
          const error = new Error("request aborted");
          error.name = "AbortError";
          reject(error);
        }, { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    (sdkListener as (code: number) => void)(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === `${endpoint}/v1/ingest`)).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(5_000);

    expect(aborted).toBe(true);
    expect(warn).toHaveBeenCalledWith(
      "[dexcost] exit-time flush exceeded 5000ms; continuing shutdown",
    );
  });

  test("beforeExit timeout aborts a stalled response body without acknowledging", async () => {
    vi.useFakeTimers();
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const before = new Set(process.listeners("beforeExit"));
    const endpoint = "https://flush-response-test.invalid";
    const tracker = init({
      apiKey: "dx_test_x",
      autoInstrument: [],
      trackHttp: false,
      endpoint,
    });
    const sdkListener = process.listeners("beforeExit").find((listener) => !before.has(listener));
    expect(sdkListener).toBeDefined();

    await tracker.track({ taskType: "stalled-response-body" }, async () => {});
    const markTasksSynced = vi.spyOn(tracker.buffer, "markTasksSynced");

    let bodyReadAborted = false;
    const fetchMock = vi.fn((url: string | URL | Request, init?: RequestInit) => {
      if (String(url) !== `${endpoint}/v1/ingest`) {
        return Promise.resolve({ ok: true, status: 202, json: async () => ({}) } as Response);
      }
      const signal = init?.signal;
      return Promise.resolve({
        ok: true,
        status: 202,
        json: () => new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            bodyReadAborted = true;
            const error = new Error("body read aborted");
            error.name = "AbortError";
            reject(error);
          }, { once: true });
        }),
      } as Response);
    });
    vi.stubGlobal("fetch", fetchMock);

    (sdkListener as (code: number) => void)(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === `${endpoint}/v1/ingest`)).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(5_000);

    expect(bodyReadAborted).toBe(true);
    expect(markTasksSynced).not.toHaveBeenCalled();
  });
});
