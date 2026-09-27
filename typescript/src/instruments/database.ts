/** Native database usage only. Prices and allocation policy live on the server. */
import { getCurrentTask } from "../core/context.js";
import { randomUUID } from "node:crypto";
import { createCostEvent, type Task } from "../core/models.js";
import type { CostTracker } from "../core/tracker.js";

export interface DatabaseResource { billingAccountId: string; resourceId: string }
export function databaseResourceId(billingAccountId: string, resourceId: string): string {
  const valid = /^[A-Za-z0-9._-]{1,100}$/;
  if (!valid.test(billingAccountId) || !valid.test(resourceId)) {
    throw new Error("Use cloud account/resource IDs (1-100 letters, digits, '.', '_' or '-')");
  }
  return `${billingAccountId}/${resourceId}`;
}
const READ = new Set("find count distinct getmore get mget hget hmget hgetall exists scan hscan sscan zscan smembers lrange zrange ft.search ft.aggregate json.get".split(" "));
const WRITE = new Set("insert update delete findandmodify bulkwrite set mset hset del unlink incr incrby decr decrby lpush rpush sadd zadd expire json.set".split(" "));
function category(command: unknown): string {
  const name = typeof command === "string" ? command.toLowerCase() : "";
  return READ.has(name) ? "read" : WRITE.has(name) ? "write" : "other";
}
type Start = { task: Task | undefined; occurredAt: Date; clock: number };
class Recorder {
  active = true;
  readonly resource: string;
  constructor(readonly tracker: CostTracker, readonly service: string, config: DatabaseResource) {
    this.resource = databaseResourceId(config.billingAccountId, config.resourceId);
  }
  start(): Start { return { task: getCurrentTask(), occurredAt: new Date(), clock: performance.now() }; }
  finish(start: Start, operation: string, quantity: number, failed: boolean): void {
    if (!this.active || !start.task || quantity < 1) return;
    try {
      this.tracker.buffer.addEvent(createCostEvent({
        eventId: randomUUID(), taskId: start.task.taskId, occurredAt: start.occurredAt, eventType: "external_cost",
        costConfidence: "unknown", provider: this.service, serviceName: "database",
        latencyMs: Math.max(0, Math.floor(performance.now() - start.clock)),
        details: {
          attribution_component: "storage",
          attribution_resource_type: "endpoint", attribution_resource_id: this.resource,
          attribution_operation_name: `database.${operation}`,
          attribution_operation_status: failed ? "failed" : "succeeded",
          attribution_usage_lines: [{ metric: `${this.service}.commands`, quantity: String(quantity), unit: "Commands" }],
          database_capture_version: "1", database_capture_basis: "observed_commands_not_billed_usage",
        },
      }));
    } catch { /* Telemetry may not change a database result or log payloads. */ }
  }
}
interface MongoClientEvents {
  readonly options?: { monitorCommands?: boolean };
  on(event: string, listener: (...args: any[]) => void): unknown;
  off(event: string, listener: (...args: any[]) => void): unknown;
}
const mongoClients = new WeakSet<object>();
/** Create MongoClient with monitorCommands:true, then attach before operations. */
export function instrumentMongoClient(client: MongoClientEvents, tracker: CostTracker, config: DatabaseResource): () => void {
  if (client.options?.monitorCommands === false) throw new Error("Create MongoClient with monitorCommands:true");
  if (mongoClients.has(client)) throw new Error("MongoDB client is already instrumented");
  const recorder = new Recorder(tracker, "mongodb_atlas", config);
  const pending = new Map<string, { start: Start; category: string }>();
  const key = (event: any): string => JSON.stringify([event.connectionId, event.requestId]);
  const started = (event: any): void => {
    const start = recorder.start();
    if (!recorder.active || !start.task) return;
    pending.set(key(event), { start, category: category(event.commandName) });
    if (pending.size > 1024) pending.delete(pending.keys().next().value!);
  };
  const finish = (event: any, failed: boolean): void => {
    const id = key(event), saved = pending.get(id);
    pending.delete(id);
    if (saved) recorder.finish(saved.start, saved.category, 1, failed);
  };
  const succeeded = (event: any): void => finish(event, Boolean(event.reply?.writeErrors?.length || event.reply?.writeConcernError));
  const failed = (event: any): void => finish(event, true);
  client.on("commandStarted", started); client.on("commandSucceeded", succeeded); client.on("commandFailed", failed);
  mongoClients.add(client);
  let closed = false;
  return () => {
    if (closed) return;
    closed = true; recorder.active = false; pending.clear(); mongoClients.delete(client);
    client.off("commandStarted", started); client.off("commandSucceeded", succeeded); client.off("commandFailed", failed);
  };
}
const redisClients = new WeakSet<object>();
export interface InstrumentedRedisClient<T> { client: T; close(): void }
export function uninstrumentRedisClient<T>(instrumentation: InstrumentedRedisClient<T>): void {
  instrumentation.close();
}
/** Use the returned node-redis proxy. Cluster, PubSub and the original client are not captured. */
export function instrumentRedisClient<T extends object>(client: T, tracker: CostTracker, config: DatabaseResource): InstrumentedRedisClient<T> {
  if (redisClients.has(client)) throw new Error("Redis client is already instrumented");
  const recorder = new Recorder(tracker, "redis_cloud", config);
  redisClients.add(client);
  const observe = (fn: () => any, operation: string, quantity: number): any => {
    const start = recorder.start();
    try {
      const result = fn();
      if (result && typeof result.then === "function") {
        return Promise.resolve(result).then((value) => {
          recorder.finish(start, operation, quantity, Array.isArray(value) && value.some((v) => v instanceof Error));
          return value;
        }, (error: unknown) => { recorder.finish(start, operation, quantity, true); throw error; });
      }
      recorder.finish(start, operation, quantity, false);
      return result;
    } catch (error) { recorder.finish(start, operation, quantity, true); throw error; }
  };
  function proxy(target: any, batch?: { count: number; target?: any; proxy?: any }, prefix = ""): any {
    let wrapped: any;
    // Driver namespaces (ft/json) are non-configurable own properties. Proxy a
    // facade, not the driver itself, to honor JavaScript proxy invariants while
    // still binding every function/private-field receiver to the actual client.
    wrapped = new Proxy(Object.create(Object.getPrototypeOf(target)), {
      set(_facade, property, value) { return Reflect.set(target, property, value, target); },
      has(_facade, property) { return Reflect.has(target, property); },
      ownKeys() { return Reflect.ownKeys(target); },
      getOwnPropertyDescriptor(_facade, property) {
        const descriptor = Reflect.getOwnPropertyDescriptor(target, property);
        return descriptor ? { ...descriptor, configurable: true } : undefined;
      },
      get(_facade, property) {
        const object = target;
        const value = Reflect.get(object, property, object);
        if ((property === "ft" || property === "json") && value && typeof value === "object") {
          return proxy(value, batch, `${String(property)}.`);
        }
        if (typeof value !== "function" || typeof property !== "string") return value;
        if (!batch && property === "multi") return (...args: any[]) => {
          const target = value.apply(object, args);
          const state = { count: 0, target, proxy: undefined as any };
          state.proxy = proxy(target, state);
          return state.proxy;
        };
        if (batch && ["exec", "EXEC", "execTyped", "execAsPipeline", "execAsPipelineTyped"].includes(property)) return (...args: any[]) => {
          // node-redis retains its queue after exec; replay executes it again.
          return observe(() => value.apply(object, args), "batch", batch.count);
        };
        const command = `${prefix}${property}`.toLowerCase();
        const raw = property === "sendCommand" || property === "addCommand";
        if (!raw && !READ.has(command) && !WRITE.has(command)) return (...args: any[]) => {
          const result = value.apply(object, args);
          if (batch && result === batch.target) return batch.proxy;
          return result === object ? wrapped : result;
        };
        return (...args: any[]) => {
          if (batch) {
            const result = value.apply(object, args); batch.count++;
            return result === batch.target ? batch.proxy : result === object ? wrapped : result;
          }
          return observe(() => value.apply(object, args), category(raw ? args[0]?.[0] : command), 1);
        };
      },
    });
    return wrapped;
  }
  const wrapped = proxy(client) as T;
  redisClients.add(wrapped);
  let closed = false;
  return { client: wrapped, close() {
    if (closed) return;
    closed = true; recorder.active = false; redisClients.delete(client); redisClients.delete(wrapped);
  } };
}
