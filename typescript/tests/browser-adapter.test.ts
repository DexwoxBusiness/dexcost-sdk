/**
 * Tests for the browser (Playwright) cost adapter.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  trackBrowser,
  getBrowserEvents,
  clearBrowserEvents,
  setBrowserBuffer,
} from "../src/adapters/browser.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { randomUUID } from "node:crypto";

/** Minimal Playwright-Page stand-in (the adapter is duck-typed). */
const fakePage = { url: "https://example.com/page" };

describe("trackBrowser", () => {
  beforeEach(() => {
    clearBrowserEvents();
    setBrowserBuffer(null);
  });

  it("records a compute_cost event when run inside a task", async () => {
    const task = createTask({ taskId: randomUUID(), taskType: "scrape" });

    const result = await runWithTask(task, () =>
      trackBrowser(
        fakePage,
        async () => {
          return "done";
        },
        { ratePerMinute: 0.6 },
      ),
    );

    expect(result).toBe("done");

    const events = getBrowserEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("compute_cost");
    expect(events[0].serviceName).toBe("playwright_browser");
    expect(events[0].taskId).toBe(task.taskId);
    expect(events[0].details["page_url"]).toBe("https://example.com/page");
    // Cost is non-negative and the task aggregate was updated.
    expect(events[0].costUsd.toNumber()).toBeGreaterThanOrEqual(0);
    expect(task.computeCostUsd.toString()).toBe(events[0].costUsd.toString());
  });

  it("is a silent pass-through when there is no active task", async () => {
    const result = await trackBrowser(fakePage, async () => 42);
    expect(result).toBe(42);
    expect(getBrowserEvents()).toHaveLength(0);
  });

  it("still records the cost when the wrapped work throws", async () => {
    const task = createTask({ taskId: randomUUID(), taskType: "scrape" });

    await expect(
      runWithTask(task, () =>
        trackBrowser(fakePage, async () => {
          throw new Error("navigation failed");
        }),
      ),
    ).rejects.toThrow("navigation failed");

    // The compute cost is still recorded even on failure.
    expect(getBrowserEvents()).toHaveLength(1);
  });

  it("persists the event to the durable buffer so the pusher can sync it", async () => {
    const buffer = new EventBuffer(":memory:");
    setBrowserBuffer(buffer);
    try {
      const task = createTask({ taskId: randomUUID(), taskType: "scrape" });
      buffer.upsertTask(task); // parent task row required by the FK constraint

      await runWithTask(task, () =>
        trackBrowser(fakePage, async () => "ok", { ratePerMinute: 0.6 }),
      );

      const stored = buffer.queryEvents(task.taskId);
      expect(stored).toHaveLength(1);
      expect(stored[0].eventType).toBe("compute_cost");
      expect(stored[0].serviceName).toBe("playwright_browser");
      // Still available via the in-memory list for lightweight setups.
      expect(getBrowserEvents()).toHaveLength(1);
    } finally {
      setBrowserBuffer(null);
      buffer.close();
    }
  });
});
