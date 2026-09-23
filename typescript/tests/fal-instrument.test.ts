import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { createTask } from "../src/core/models.js";
import { runWithTask } from "../src/core/context.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  instrumentFal,
  provideFalModule,
  uninstrumentFal,
} from "../src/instruments/fal.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-fal-test-"));
});

afterEach(() => {
  uninstrumentFal();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("fal instrumentation", () => {
  it("preserves image evidence without applying the stale SDK per-image price", async () => {
    const fakeFal = {
      run(_endpointId: string, _options: unknown) {
        return {
          requestId: "fal-request-1",
          data: {
            images: [
              { url: "https://private.example/one.png", width: 1280, height: 1024 },
              { url: "https://private.example/two.png", width: 1280, height: 1024 },
            ],
            prompt: "private-output-prompt",
          },
        };
      },
    };
    const buffer = new EventBuffer(join(tmpDir, "fal.db"));
    const pricing = new PricingEngine();
    provideFalModule(fakeFal);
    await instrumentFal(pricing, buffer);
    try {
      const task = createTask({ taskId: randomUUID(), taskType: "fal-test" });
      await runWithTask(task, async () => {
        fakeFal.run("fal-ai/flux/schnell", {
          input: { prompt: "private-input-prompt", num_images: 2 },
        });
      });

      const events = buffer.getAllEvents();
      expect(events).toHaveLength(1);
      expect(events[0]).toMatchObject({
        provider: "fal_ai",
        model: "fal_ai/fal-ai/flux/schnell",
        costConfidence: "unknown",
        pricingSource: "unknown",
      });
      expect(events[0].costUsd.toString()).toBe("0");
      expect(events[0].details["provider_record_id"]).toBe("fal-request-1");
      expect(toAttributionObservationV3(events[0])?.provider).toEqual({
        name: "fal_ai",
        service: "inference",
        record_id: "fal-request-1",
      });
      expect(events[0].details["attribution_usage_lines"]).toEqual([
        { metric: "output_image_count", quantity: "2", unit: "Images" },
      ]);
      expect(events[0].details["attribution_dimensions"]).toEqual(expect.arrayContaining([
        { key: "output_width", value: { type: "string", value: "1280" } },
        { key: "output_height", value: { type: "string", value: "1024" } },
      ]));
      expect(JSON.stringify(events[0])).not.toContain("private-");
    } finally {
      buffer.close();
    }
  });

  it("uses the same request identity service for queued jobs", async () => {
    const fakeFal = {
      queue: {
        submit() {
          return { request_id: "fal-queued-1" };
        },
      },
    };
    const buffer = new EventBuffer(join(tmpDir, "fal-queue.db"));
    const pricing = new PricingEngine();
    provideFalModule(fakeFal);
    await instrumentFal(pricing, buffer);
    try {
      const task = createTask({ taskId: randomUUID(), taskType: "fal-queue-test" });
      runWithTask(task, () => {
        fakeFal.queue.submit("fal-ai/flux/schnell", { input: { prompt: "private" } });
      });

      expect(buffer.getPendingLedger("provider_job")).toEqual([
        expect.objectContaining({
          provider: "fal_ai",
          service: "inference",
          provider_record_id: "fal-queued-1",
          status: "submitted",
        }),
      ]);
    } finally {
      buffer.close();
    }
  });

  it("preserves the official stream object's request identity", async () => {
    const fakeFal = {
      stream() {
        return {
          requestId: "fal-stream-1",
          async *[Symbol.asyncIterator]() {
            yield { audio: { duration: 2.5, url: "https://private.example/audio.mp3" } };
          },
        };
      },
    };
    const buffer = new EventBuffer(join(tmpDir, "fal-stream.db"));
    const pricing = new PricingEngine();
    provideFalModule(fakeFal);
    await instrumentFal(pricing, buffer);
    try {
      const task = createTask({ taskId: randomUUID(), taskType: "fal-stream-test" });
      await runWithTask(task, async () => {
        for await (const _chunk of fakeFal.stream("fal-ai/audio-model", { input: {} })) {
          // Consumption finalizes the provider observation.
        }
      });

      const event = buffer.getAllEvents()[0];
      expect(toAttributionObservationV3(event)?.provider).toEqual({
        name: "fal_ai",
        service: "inference",
        record_id: "fal-stream-1",
      });
    } finally {
      buffer.close();
    }
  });
});
