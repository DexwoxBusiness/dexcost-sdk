/**
 * B14 regression — Sprint 2 Theme D / plan §3.2.3.
 *
 * After 401/403 the EventPusher sets `_authFailed=true` and stops.
 * `setApiKey` is the public path back to a working push loop.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { init, close, setApiKey } from "../src/index.js";
import { EventBuffer } from "../src/transport/buffer.js";

describe("setApiKey (B14)", () => {
  afterEach(() => {
    try {
      close();
    } catch {
      // already closed
    }
    EventBuffer._forceFallbackForTest = false;
    vi.restoreAllMocks();
  });

  test("returns false and warns when called before init", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const ok = setApiKey("dx_live_new");
    expect(ok).toBe(false);
    expect(warnSpy).toHaveBeenCalled();
  });

  test("returns true and updates the tracker's apiKey after init", () => {
    EventBuffer._forceFallbackForTest = true; // avoid native binding requirement
    vi.spyOn(console, "warn").mockImplementation(() => {});

    const tracker = init({ apiKey: "dx_test_old" });
    expect((tracker as unknown as { _config: { apiKey: string } })._config.apiKey).toBe(
      "dx_test_old",
    );

    const ok = setApiKey("dx_live_new");
    expect(ok).toBe(true);
    expect((tracker as unknown as { _config: { apiKey: string } })._config.apiKey).toBe(
      "dx_live_new",
    );
  });

  test("clears the pusher _authFailed flag", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});

    const tracker = init({ apiKey: "dx_test_old" });
    const pusher = (tracker as unknown as { _pusher: { _authFailed: boolean } | null })
      ._pusher;
    if (pusher == null) {
      // local-only mode (no cloud); contract still holds — function shouldn't throw.
      expect(setApiKey("dx_live_new")).toBe(true);
      return;
    }
    // Simulate auth failure.
    pusher._authFailed = true;
    setApiKey("dx_live_new");
    expect(pusher._authFailed).toBe(false);
  });
});
