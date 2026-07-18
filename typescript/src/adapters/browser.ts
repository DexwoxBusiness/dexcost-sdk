/**
 * Browser cost adapter — automatic cost tracking for Playwright sessions.
 *
 * `trackBrowser` wraps a block of Playwright work, measures wall-clock
 * time, and records a `compute_cost` event proportional to session
 * duration. Mirrors the Python SDK's `adapters/browser.py`.
 *
 * `playwright` is an optional peer dependency — this adapter is duck-typed
 * and never imports it, so it works with any object exposing a `.url`.
 *
 * Usage:
 *
 *   import { trackBrowser } from "dexcost";
 *
 *   await tracker.track({ taskType: "scrape" }, async () => {
 *     await trackBrowser(page, async () => {
 *       await page.goto("https://example.com");
 *     }, { ratePerMinute: 0.01 });
 *   });
 */

import { randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent, Decimal, type CostEvent } from "../core/models.js";
import { scrubUrl } from "../security/redaction.js";
import type { EventBuffer } from "../transport/buffer.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Default browser usage rate in USD per minute. */
const DEFAULT_RATE_PER_MINUTE = 0.01;

/** Events recorded by the browser adapter. */
/** Events recorded by the browser adapter.
 *
 * Sprint 4 §5.2 (A3) — hard FIFO cap matching Python (c1d87a7) and
 * the TS http adapter. Long-running Playwright sessions otherwise
 * leak ~250 bytes per recording.
 */
const _RECORDED_EVENTS_CAP = 10_000;
const _recordedEvents: CostEvent[] = [];

/**
 * Durable storage buffer wired by {@link setBrowserBuffer}. When set, recorded
 * browser cost events are persisted (and shipped by the EventPusher) instead
 * of only being kept in the in-memory `_recordedEvents` list.
 */
let _buffer: EventBuffer | null = null;

/**
 * Wire the browser adapter to a storage buffer. The CostTracker calls this on
 * construction so `trackBrowser()` cost events reach SQLite and the sync
 * pusher. Pass `null` to detach (events then stay in-memory only).
 */
export function setBrowserBuffer(buffer: EventBuffer | null): void {
  _buffer = buffer;
}

/** Return all events recorded by the browser adapter since the last clear. */
export function getBrowserEvents(): CostEvent[] {
  return [..._recordedEvents];
}

/** Clear the browser adapter's recorded events list. */
export function clearBrowserEvents(): void {
  _recordedEvents.length = 0;
}

/** Options for {@link trackBrowser}. */
export interface TrackBrowserOptions {
  /** Cost per minute of browser usage in USD. Defaults to `0.01`. */
  ratePerMinute?: number;
}

/**
 * Run `fn` while measuring browser session wall-clock time, then record a
 * `compute_cost` event with `costUsd = elapsedMinutes * ratePerMinute`.
 *
 * The event is only recorded when there is an active task in the context
 * (via `getCurrentTask()`). When no task is active, the timing wrapper is
 * a silent pass-through. The cost is always recorded even if `fn` throws.
 *
 * @param page - A Playwright `Page` (or any object with a `.url`). Not
 *   type-checked, to avoid requiring `playwright` as a hard dependency.
 * @param fn - The browser work to run and time.
 * @param options - Optional `ratePerMinute` override.
 */
export async function trackBrowser<T>(
  page: any,
  fn: () => Promise<T>,
  options: TrackBrowserOptions = {},
): Promise<T> {
  const ratePerMinute = options.ratePerMinute ?? DEFAULT_RATE_PER_MINUTE;
  const start = performance.now();
  try {
    return await fn();
  } finally {
    const elapsedSeconds = (performance.now() - start) / 1000;
    _recordBrowserEvent(page, elapsedSeconds, ratePerMinute);
  }
}

/** Record a compute_cost event for a browser session. No-op without a task. */
function _recordBrowserEvent(
  page: any,
  elapsedSeconds: number,
  ratePerMinute: number,
): void {
  const task = getCurrentTask();
  if (task === undefined) {
    return;
  }

  // Exact decimal: wall-clock minutes × rate. Inputs are routed through
  // String() so a float64 elapsedSeconds / ratePerMinute never poisons it
  // (matches Python's Decimal(str(x))).
  const costUsd = new Decimal(String(elapsedSeconds))
    .dividedBy(60)
    .times(new Decimal(String(ratePerMinute)));

  let pageUrl = "";
  try {
    const url: unknown = page?.url;
    const raw = typeof url === "function" ? String(url.call(page)) : String(url ?? "");
    pageUrl = scrubUrl(raw);
  } catch {
    // page may have closed — ignore
  }

  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "compute_cost",
    costUsd,
    costConfidence: "computed",
    // The rate is supplied by the caller (or the SDK default), not selected
    // from the versioned rate registry. Attribute it as manual evidence.
    pricingSource: "manual",
    serviceName: "playwright_browser",
    isRetry: false,
    details: {
      wall_clock_seconds: Number(elapsedSeconds.toFixed(6)),
      rate_per_minute: ratePerMinute,
      page_url: pageUrl,
    },
  });

  task.computeCostUsd = task.computeCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  _recordedEvents.push(event);
  if (_recordedEvents.length > _RECORDED_EVENTS_CAP) {
    _recordedEvents.splice(0, _RECORDED_EVENTS_CAP / 10);
  }

  // Persist durably so the EventPusher ships the browser cost; the in-memory
  // list above is kept for tests and lightweight setups.
  if (_buffer) {
    _buffer.addEvent(event);
  }
}
