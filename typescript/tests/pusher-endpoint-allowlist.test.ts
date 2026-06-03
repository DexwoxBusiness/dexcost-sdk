/**
 * Security hardening — telemetry pusher uses only the explicitly configured
 * endpoint, never the process env.
 *
 * The pusher used to call `resolveEndpoint()` itself, which read
 * `DEXCOST_ENDPOINT` from the env. A hostile env
 * (`DEXCOST_ENDPOINT=http://evil.example`) could redirect telemetry plus the
 * `Authorization: Bearer <apiKey>` header to an attacker. The pusher now
 * receives a fully-resolved endpoint from the tracker (explicit in-code config
 * or the hardcoded default) and never reads the env, so that vector is closed.
 *
 * These tests drive a real push and assert on the URL passed to the mocked
 * `fetch` — proving the endpoint comes from config and the Bearer key never
 * reaches an env-controlled host.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import { resolveEndpoint, DEFAULT_ENDPOINT } from "../src/core/endpoint.js";
import { createCostEvent } from "../src/core/models.js";

function makeEvent() {
  return createCostEvent({
    eventId: randomUUID(),
    taskId: randomUUID(),
    eventType: "llm_call",
    costUsd: 0.05,
    costConfidence: "exact",
    pricingSource: "litellm",
    provider: "openai",
    model: "gpt-4",
    inputTokens: 100,
    outputTokens: 50,
  });
}

describe("Pusher endpoint config (security hardening)", () => {
  let tmpDir: string;
  let buffer: EventBuffer;
  const originalFetch = globalThis.fetch;
  const ORIGINAL_ENDPOINT = process.env.DEXCOST_ENDPOINT;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-allowlist-"));
    buffer = new EventBuffer(join(tmpDir, "test.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(tmpDir, { recursive: true, force: true });
    globalThis.fetch = originalFetch;
    // Restore the original env var so other test files are unaffected.
    if (ORIGINAL_ENDPOINT === undefined) {
      delete process.env.DEXCOST_ENDPOINT;
    } else {
      process.env.DEXCOST_ENDPOINT = ORIGINAL_ENDPOINT;
    }
    vi.restoreAllMocks();
  });

  it("POSTs to the https default when no endpoint is configured", async () => {
    buffer.addEvent(makeEvent());

    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ queued: 1 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    // Mirror the tracker: resolve from (absent) explicit config → default.
    const endpoint = resolveEndpoint(undefined);
    const pusher = new EventPusher(
      buffer,
      { apiKey: "dx_live_secret", batchSize: 100, flushIntervalMs: 60_000 },
      endpoint,
    );

    await pusher.flush();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toBe(`${DEFAULT_ENDPOINT}/v1/ingest`);
    expect(url).toBe("https://api.dexcost.io/v1/ingest");
  });

  it("honours an explicit configured endpoint end-to-end", async () => {
    buffer.addEvent(makeEvent());

    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ queued: 1 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    const endpoint = resolveEndpoint("https://custom.example");
    const pusher = new EventPusher(
      buffer,
      { apiKey: "dx_live_secret", batchSize: 100, flushIntervalMs: 60_000 },
      endpoint,
    );

    await pusher.flush();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toBe("https://custom.example/v1/ingest");
  });

  it("IGNORES DEXCOST_ENDPOINT env — Bearer key never hits the env host", async () => {
    // Simulate a hostile process env. The SDK must not read it at all.
    process.env.DEXCOST_ENDPOINT = "http://evil.example";

    buffer.addEvent(makeEvent());

    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ queued: 1 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    // Init WITHOUT an explicit endpoint option → resolves to the default,
    // completely ignoring the env var.
    const endpoint = resolveEndpoint(undefined);
    const pusher = new EventPusher(
      buffer,
      { apiKey: "dx_live_secret", batchSize: 100, flushIntervalMs: 60_000 },
      endpoint,
    );

    await pusher.flush();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0]?.[0] as string;
    // Still the production default — the env had zero effect.
    expect(url).toBe("https://api.dexcost.io/v1/ingest");
    // The hostile env host must never be contacted (Bearer key never leaks).
    expect(url.startsWith("http://evil.example")).toBe(false);
    expect(url).not.toContain("evil.example");
  });
});
