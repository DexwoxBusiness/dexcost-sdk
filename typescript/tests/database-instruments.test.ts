import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { MongoClient } from "mongodb";
import { createClient } from "redis";
import { describe, it, expect, vi } from "vitest";
import { instrumentMongoClient, instrumentRedisClient, databaseResourceId } from "../src/instruments/database.js";
import { createTask, type CostEvent } from "../src/core/models.js";
import { runWithTask } from "../src/core/context.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import type { CostTracker } from "../src/core/tracker.js";

const cases = JSON.parse(readFileSync(new URL("../../fixtures/native_database_conformance.json", import.meta.url), "utf8")).cases;
const config = { billingAccountId: "acct", resourceId: "db" };
function recording() {
  const events: CostEvent[] = [];
  const tracker = { buffer: { addEvent: (e: CostEvent) => { events.push(e); return true; } } } as unknown as CostTracker;
  const task = createTask({ taskId: randomUUID(), taskType: "agent" });
  return { events, tracker, task };
}

describe("paired native database capture", () => {
  it.each(cases)("shared corpus $id", async (c: any) => {
    const { events, tracker, task } = recording();
    await runWithTask(task, async () => {
      if (c.service === "mongodb_atlas") {
        const client = new MongoClient("mongodb://localhost:27017", { monitorCommands: true });
        const close = instrumentMongoClient(client, tracker, config);
        expect(() => instrumentMongoClient(client, tracker, config)).toThrow("already");
        const event = { connectionId: "host:27017", requestId: 1, commandName: c.command, command: { secret: "value" } };
        client.emit("commandStarted", event);
        client.emit(c.failed ? "commandFailed" : "commandSucceeded", event);
        client.emit("commandSucceeded", event);
        close(); await client.close();
      } else {
        const original = { async sendCommand(_args: string[]) {
          if (c.failed) throw new Error("secret-key and private-response");
          return "private-response";
        } };
        const wrapped = instrumentRedisClient(original, tracker, config);
        const promise = wrapped.client.sendCommand([c.command, "secret-key"]);
        if (c.failed) await expect(promise).rejects.toThrow();
        else expect(await promise).toBe("private-response");
        wrapped.close();
      }
    });
    expect(events).toHaveLength(1);
    const ev = events[0], observation = toAttributionObservationV3(ev)!;
    expect(observation.provider).toEqual({ name: c.service, service: "database" });
    expect(observation.resource).toEqual({ type: "endpoint", id: "acct/db" });
    expect(observation.usage[0]).toMatchObject({ metric: `${c.service}.commands`, quantity: "1", unit: "Commands" });
    expect(observation.operation).toMatchObject({ name: `database.${c.category}`, status: c.failed ? "failed" : "succeeded" });
    expect(ev.costConfidence).toBe("unknown");
    expect(observation.cost_evidence).toBeUndefined();
    expect(JSON.stringify(ev)).not.toMatch(/secret|private-response/);
  });

  it("keeps the task captured at command start, across concurrent completions", async () => {
    const { events, tracker, task } = recording();
    const other = createTask({ taskId: randomUUID(), taskType: "other" });
    const client = new MongoClient("mongodb://localhost:27017", { monitorCommands: true });
    const close = instrumentMongoClient(client, tracker, config);
    const a = { requestId: 1, connectionId: "host", commandName: "find" };
    const b = { ...a, requestId: 2 };
    runWithTask(task, () => client.emit("commandStarted", a));
    runWithTask(other, () => client.emit("commandStarted", b));
    client.emit("commandSucceeded", b); client.emit("commandFailed", a);
    expect(events.map((e) => e.taskId)).toEqual([other.taskId, task.taskId]);
    close(); await client.close();
  });

  it("does not count queued pipelines, counts execution once, preserves partial failure", async () => {
    const { events, tracker, task } = recording();
    class Pipeline {
      set(..._args: unknown[]) { return this; }
      get(..._args: unknown[]) { return this; }
      async exec() { return ["OK", new Error("private")]; }
    }
    const original = { multi: () => new Pipeline() };
    const tracked = instrumentRedisClient(original, tracker, config);
    expect(() => instrumentRedisClient(original, tracker, config)).toThrow("already");
    await runWithTask(task, async () => {
      const batch = tracked.client.multi().set("private", "private").get("private");
      expect(events).toHaveLength(0);
      await batch.exec();
      expect(events).toHaveLength(1);
      expect(events[0].details.attribution_usage_lines).toEqual([{ metric: "redis_cloud.commands", quantity: "2", unit: "Commands" }]);
      expect(events[0].details.attribution_operation_status).toBe("failed");
      tracked.close(); await batch.exec(); expect(events).toHaveLength(1);
    });
  });

  it("supports real node-redis method dispatch and fails open when the recorder fails", async () => {
    const { events, tracker, task } = recording();
    const client = createClient();
    // Real disconnected commands reject without touching the network; the proxy
    // must bind the driver's private-field receiver correctly and retain errors.
    const tracked = instrumentRedisClient(client, tracker, config);
    await expect(tracked.client.get("private")).rejects.toThrow();
    expect(events).toHaveLength(0); // no ambient task
    await runWithTask(task, async () => {
      await expect(tracked.client.get("private")).rejects.toThrow();
      expect(events).toHaveLength(1);
      vi.spyOn(tracker.buffer, "addEvent").mockImplementation(() => { throw new Error("storage"); });
      await expect(tracked.client.set("private", "private")).rejects.toThrow();
    });
    tracked.close();
  });

  it("keeps real node-redis module pipeline chaining, aliases and queue replay observable", async () => {
    const { events, tracker, task } = recording();
    const original = createClient();
    const tracked = instrumentRedisClient(original, tracker, config);
    await runWithTask(task, async () => {
      const pipeline = tracked.client.multi().ft.search("private-index", "private-query").get("private-key");
      expect(events).toHaveLength(0);
      await expect(pipeline.execAsPipeline()).rejects.toThrow();
      await expect(pipeline.execTyped()).rejects.toThrow();
      expect(events).toHaveLength(2);
      for (const event of events) expect(event.details.attribution_usage_lines).toEqual([
        { metric: "redis_cloud.commands", unit: "Commands", quantity: "2" },
      ]);
      expect(JSON.stringify(events)).not.toMatch(/private-/);
    });
    tracked.close();
  });

  it.each(["", "user:password@host", "https://host/db", " x", "a".repeat(101)])("rejects connection strings %s", (id) => {
    expect(() => databaseResourceId("acct", id)).toThrow();
  });
});
