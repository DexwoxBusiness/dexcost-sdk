import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  instrumentPerplexity,
  providePerplexityModule,
  uninstrumentPerplexity,
} from "../src/instruments/perplexity.js";

class FakeAPIPromise<T> implements PromiseLike<T> {
  constructor(private readonly value: T) {}
  then<TResult1 = T, TResult2 = never>(
    fulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null,
    rejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    return Promise.resolve(this.value).then(fulfilled, rejected);
  }
  asResponse(): Promise<Response> { return Promise.resolve(new Response("ok")); }
  withResponse(): Promise<{ data: T; response: Response }> {
    return Promise.resolve({ data: this.value, response: new Response("ok") });
  }
}

class FakePerplexity {
  readonly chat = {
    completions: {
      create: (body: unknown) => this.post("/chat/completions", { body }),
    },
  };
  readonly responses = {
    create: (body: unknown) => this.post("/responses", { body }),
    retrieve: (id: string) => this.get(`/responses/${id}`),
    cancel: (id: string) => this.post(`/responses/${id}/cancel`),
  };
  post(path: string, options?: { body?: any }): FakeAPIPromise<any> {
    if (path === "/chat/completions") return new FakeAPIPromise({
      id: "pplx-chat", model: options?.body?.model,
      usage: { prompt_tokens: 41, completion_tokens: 8, cost: { total_cost: 0.002 } },
    });
    if (path === "/responses") return new FakeAPIPromise({
      id: "resp-bg", model: options?.body?.model, status: "queued",
    });
    if (path.endsWith("/cancel")) return new FakeAPIPromise({ id: "resp-bg", status: "cancelled" });
    return new FakeAPIPromise({});
  }
  get(path: string): FakeAPIPromise<any> {
    if (path === "/responses/resp-bg") return new FakeAPIPromise({
      id: "resp-bg", model: "sonar-pro", status: "completed",
      usage: { input_tokens: 90, output_tokens: 11, cost: { total_cost: 0.006 } },
    });
    return new FakeAPIPromise({});
  }
}

describe("current official Perplexity attribution", () => {
  let directory: string;
  let buffer: EventBuffer;
  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-perplexity-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    providePerplexityModule({ Perplexity: FakePerplexity, default: FakePerplexity });
  });
  afterEach(() => {
    uninstrumentPerplexity();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("uses the official transport surface and preserves APIPromise helpers", async () => {
    await instrumentPerplexity(new PricingEngine(), buffer);
    const promise = new FakePerplexity().chat.completions.create({ model: "sonar-pro" });
    expect(typeof promise.asResponse).toBe("function");
    expect(typeof promise.withResponse).toBe("function");
    const response = await promise;
    expect(response.id).toBe("pplx-chat");
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "perplexity", serviceName: "sonar", model: "perplexity/sonar-pro",
      inputTokens: 41, outputTokens: 8, costConfidence: "exact",
    });
  });

  it("persists and reconciles background Responses jobs", async () => {
    await instrumentPerplexity(new PricingEngine(), buffer);
    const client = new FakePerplexity();
    await client.responses.create({ model: "sonar-pro", background: true });
    expect(buffer.getProviderJob("perplexity", "responses", "resp-bg")?.status).toBe("submitted");
    await client.responses.retrieve("resp-bg");
    const final = buffer.getProviderJob("perplexity", "responses", "resp-bg");
    expect(final).toMatchObject({ status: "succeeded", revision: 2 });
    expect(final?.usage).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_tokens", quantity: "90" }),
      expect.objectContaining({ metric: "output_tokens", quantity: "11" }),
    ]));
  });
});
