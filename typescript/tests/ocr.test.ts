import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { afterEach, describe, expect, it, vi } from "vitest";
import { instrumentTextract, instrumentDocumentAI, uninstrumentOcr } from "../src/instruments/ocr.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import type { CostTracker } from "../src/core/tracker.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { runWithProviderCapture } from "../src/instruments/provider-capture.js";

const data = JSON.parse(readFileSync(new URL("../../fixtures/ocr_conformance.json", import.meta.url), "utf8"));
const require = createRequire(import.meta.url), buffers: EventBuffer[] = [];
afterEach(() => { buffers.splice(0).forEach(b => b.close()); vi.useRealTimers(); vi.restoreAllMocks(); });
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: data.task_id, taskType: "ocr" }); buffer.upsertTask(task);
  return { tracker: { buffer } as unknown as CostTracker, task };
}
const awsScope = { billingAccountId: data.aws_payer, usageAccountId: data.aws_usage_account, region: data.region };
const googleScope = { billingAccountId: data.google_account, processorVersion: data.processor, processorType: "OCR_PROCESSOR" as const };
const awsResource = `${data.aws_payer}/${data.aws_usage_account}.${data.region}.detect_document_text`;
function nativeAws(c: any) {
  const response = { DocumentMetadata: { Pages: c.pages }, Blocks: [{ Text: "PRIVATE" }], $metadata: { requestId: c.id, httpStatusCode: c.status ?? 200, attempts: c.attempts ?? 1 } };
  class Client {
    middleware: any; config = { serviceId: "Textract", region: async () => c.region ?? data.region };
    middlewareStack = { add: (middleware: any) => { this.middleware = middleware; } };
    async send(_command: unknown) {
      for (let i = 0; i < (c.transport_attempts ?? 1); i++) await this.middleware(async () => undefined)({ request: { protocol: "https:", hostname: c.host ?? `textract.${data.region}.amazonaws.com`, path: "/", method: "POST" } });
      return response;
    }
  }
  return { client: new Client(), response, command: { constructor: { name: "DetectDocumentTextCommand" }, input: { Document: { Bytes: "PRIVATE" } } } };
}
function nativeGoogle(c: any) {
  const request = { name: c.processor ?? data.processor, rawDocument: { mimeType: "application/pdf", content: "PRIVATE" }, ...(c.options ? { processOptions: { ocrConfig: { premiumFeatures: { enableMathOcr: true } } } } : {}), ...(c.mask ? { fieldMask: { paths: ["text"] } } : {}) };
  const response = [{ document: { pages: c.pages?.map((pageNumber: unknown) => ({ pageNumber, tokens: ["PRIVATE"] })), text: "PRIVATE", error: { code: c.error ?? 0 } } }, request, {}];
  const client = { _opts: { servicePath: c.host ?? "us-documentai.googleapis.com" }, processDocument: vi.fn(async (_request: unknown, _options?: unknown) => response) };
  return { client, request, response, call: c.default_retry ? {} : { retry: null } };
}
describe("paired hosted OCR evidence", () => {
  it.each(data.textract)("Textract $id", async c => {
    const { tracker, task } = setup(), { client, response, command } = nativeAws(c);
    const wrapped = instrumentTextract(client, tracker, awsScope);
    expect(await runWithTask(task, () => wrapped.send(command))).toBe(response);
    const raw = tracker.buffer.getProviderJob("amazon_textract", "ocr", `${awsResource}/${c.id}`);
    if (!c.capture) { expect(raw).toBeUndefined(); return; }
    const event = providerJobFromDict(raw!).toAttributionObservation();
    expect(event.usage[0]).toMatchObject({ metric: "amazon_textract.detect_document_text_pages", quantity: "2", unit: "Pages" });
    expect(event.resource?.id).toBe(awsResource); expect(event).not.toHaveProperty("cost_evidence");
    expect(JSON.stringify(raw)).not.toContain("PRIVATE");
    await runWithTask(task, () => wrapped.send(command));
    expect(tracker.buffer.getProviderJob("amazon_textract", "ocr", `${awsResource}/${c.id}`)).toEqual(raw);
  });
  it.each(data.document_ai)("Document AI $id", async c => {
    const { tracker, task } = setup(), { client, response, request, call } = nativeGoogle(c);
    const wrapped = instrumentDocumentAI(client, tracker, googleScope);
    expect(await runWithTask(task, () => wrapped.processDocument(request, call))).toBe(response);
    const events = tracker.buffer.getPendingEvents(); expect(events).toHaveLength(c.capture ? 1 : 0);
    if (c.capture) {
      const event = toAttributionObservationV3(events[0])!;
      expect(event.usage[0]).toMatchObject({ metric: "google_document_ai.enterprise_ocr_pages", quantity: "2", unit: "Pages" });
      expect(event.resource?.id).toBe(`${data.google_account}/agent-project.us.abc123.enterprise_ocr`);
      expect(event).not.toHaveProperty("cost_evidence"); expect(JSON.stringify(events)).not.toContain("PRIVATE");
    }
  });
  it("preserves exact interval including zero-duration completed responses, task ownership and once-only await", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-30T23:59:59Z"));
    const { tracker, task } = setup(), { client, request, call, response } = nativeGoogle(data.document_ai[0]);
    client.processDocument = vi.fn(async () => { vi.setSystemTime(new Date("2026-10-01T00:00:01Z")); return response; });
    const wrapped = instrumentDocumentAI(client, tracker, googleScope);
    const pending = runWithTask(task, () => wrapped.processDocument(request, call));
    await pending; await pending;
    expect(tracker.buffer.getPendingEvents()).toHaveLength(1);
    expect(toAttributionObservationV3(tracker.buffer.getPendingEvents()[0])?.usage_period).toEqual({ start_at: "2026-09-30T23:59:59.000000Z", end_at: "2026-10-01T00:00:01.000000Z" });
    await runWithTask(task, () => wrapped.processDocument(request, call));
    expect(toAttributionObservationV3(tracker.buffer.getPendingEvents()[1])?.usage_period).toEqual({ start_at: "2026-10-01T00:00:01.000000Z", end_at: "2026-10-01T00:00:01.000000Z" });
  });
  it("does not observe user error recovery, no-task, disabled or nested calls", async () => {
    const { tracker, task } = setup(), { client, request, call, response } = nativeGoogle(data.document_ai[0]);
    const wrapped = instrumentDocumentAI(client, tracker, googleScope);
    await wrapped.processDocument(request, call);
    await runWithTask(task, () => runWithProviderCapture("outer", () => wrapped.processDocument(request, call)));
    const error = new Error("PRIVATE"); client.processDocument = vi.fn(async () => { throw error; });
    expect(await runWithTask(task, () => wrapped.processDocument(request, call).catch(() => response))).toBe(response);
    uninstrumentOcr(wrapped); client.processDocument = vi.fn(async () => response);
    await runWithTask(task, () => wrapped.processDocument(request, call));
    expect(tracker.buffer.getPendingEvents()).toHaveLength(0);
  });
  it("runs real AWS SDK v3 serialization/middleware with an in-memory transport", async () => {
    const { TextractClient, DetectDocumentTextCommand } = require("@aws-sdk/client-textract");
    const { tracker, task } = setup(), sent: any[] = [];
    const client = new TextractClient({ region: data.region, credentials: { accessKeyId: "synthetic", secretAccessKey: "synthetic" }, requestHandler: { handle: async (request: any) => {
      sent.push(request.hostname);
      return { response: { statusCode: 200, headers: { "x-amzn-requestid": "real-sdk" }, body: Buffer.from(JSON.stringify({ DocumentMetadata: { Pages: 2 }, Blocks: [{ Text: "PRIVATE" }] })) } };
    } } });
    const response = await runWithTask(task, () => instrumentTextract(client, tracker, awsScope).send(new DetectDocumentTextCommand({ Document: { Bytes: Buffer.from("PRIVATE") } })));
    expect((response as any).DocumentMetadata.Pages).toBe(2); expect(sent).toEqual(["textract.us-east-1.amazonaws.com"]);
    expect(tracker.buffer.getProviderJob("amazon_textract", "ocr", `${awsResource}/real-sdk`)).toBeDefined();
    client.destroy();
  });
  it("runs real Google generated processDocument with tuple response and native request routing", async () => {
    const { v1 } = require("@google-cloud/documentai");
    const { tracker, task } = setup(), { request, response } = nativeGoogle(data.document_ai[0]);
    const client = new v1.DocumentProcessorServiceClient({ apiEndpoint: "us-documentai.googleapis.com", fallback: true });
    const initialize = vi.spyOn(client, "initialize").mockResolvedValue({});
    const rpc = vi.fn(async (_req, options) => { expect(options.retry).toBeNull(); expect(options.otherArgs.headers["x-goog-request-params"]).toContain("name="); return response; });
    client.innerApiCalls.processDocument = rpc;
    const value = await runWithTask(task, () => instrumentDocumentAI(client, tracker, googleScope).processDocument(request, { retry: null }));
    expect(value[0]).toBe(response[0]); expect(initialize).toHaveBeenCalled(); expect(rpc).toHaveBeenCalledTimes(1);
    expect(tracker.buffer.getPendingEvents()).toHaveLength(1);
  });
});
