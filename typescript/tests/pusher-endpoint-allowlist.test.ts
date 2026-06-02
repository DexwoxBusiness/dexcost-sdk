/**
 * Security regression — telemetry pusher HTTPS allow-list.
 *
 * The pusher used to read `DEXCOST_ENDPOINT` raw and POST telemetry plus the
 * `Authorization: Bearer <apiKey>` header to whatever URL it found. A hostile
 * env (`DEXCOST_ENDPOINT=http://evil.example`) could exfiltrate the API key in
 * cleartext. The pusher now routes through `resolveEndpoint()` (shared with the
 * tracker), which fails closed to the https:// default and honours valid https.
 *
 * These tests drive a real push and assert on the URL passed to the mocked
 * `fetch` — proving the Bearer key never reaches an http host.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
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

describe("Pusher endpoint allow-list (security regression)", () => {
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

  it("rejects http:// and POSTs to the https default — Bearer key never hits the http host", async () => {
    // Suppress the expected rejection warning from resolveEndpoint().
    vi.spyOn(console, "warn").mockImplementation(() => {});
    process.env.DEXCOST_ENDPOINT = "http://evil.example";

    buffer.addEvent(makeEvent());

    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ queued: 1 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_secret",
      batchSize: 100,
      flushIntervalMs: 60_000,
    });

    await pusher.flush();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url.startsWith("https://api.dexcost.io/v1/ingest")).toBe(true);
    // The hostile http host must never be contacted.
    expect(url.startsWith("http://evil.example")).toBe(false);
  });

  it("honours a valid https:// endpoint", async () => {
    process.env.DEXCOST_ENDPOINT = "https://custom.example";

    buffer.addEvent(makeEvent());

    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ queued: 1 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_secret",
      batchSize: 100,
      flushIntervalMs: 60_000,
    });

    await pusher.flush();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toBe("https://custom.example/v1/ingest");
  });
});
