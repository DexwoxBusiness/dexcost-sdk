/** Hosted OCR evidence only: no document content, SDK prices or hidden calls. */
import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent, Decimal } from "../core/models.js";
import { ProviderJobRevision } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

type Native = Record<string, any>;
const states = new WeakMap<object, { active: boolean }>();
const transport = new AsyncLocalStorage<{ attempts: number; valid: boolean; host: string }>();
const MIME = new Set(["application/pdf", "image/png", "image/jpeg", "image/tiff"]);
const empty = (value: unknown): boolean => value == null || (typeof value === "object" && Object.keys(value).length === 0);
function ordinaryGoogleArgs(value: Native | undefined): boolean {
  return empty(value) || (Object.keys(value!).every(key => key === "headers") &&
    Object.keys(value!.headers ?? {}).every(key => key.toLowerCase() === "x-goog-request-params"));
}

export interface TextractBinding { billingAccountId: string; usageAccountId: string; region: string }
/** AWS SDK v3 facade. Explicit payer and credential-owner mapping; only standard
 * regional HTTPS DetectDocumentText with one successful native attempt counts.
 */
export function instrumentTextract<T extends object>(client: T, tracker: CostTracker, options: TextractBinding): T {
  const scope = { ...options };
  if (!/^\d{12}$/.test(scope.billingAccountId) || !/^\d{12}$/.test(scope.usageAccountId) || !/^[a-z]{2}(?:-[a-z]+)+-[0-9]$/.test(scope.region)) throw new Error("Textract requires payer account, credential-owner account and region");
  const resource = databaseResourceId(scope.billingAccountId, `${scope.usageAccountId}.${scope.region}.detect_document_text`);
  const state = { active: true };
  try {
    (client as Native).middlewareStack.add((next: (args: Native) => Promise<unknown>) => (args: Native) => {
      const evidence = transport.getStore();
      if (evidence) {
        evidence.attempts++;
        const request = args.request;
        evidence.valid = request?.protocol === "https:" && request.hostname === evidence.host && (request.port === undefined || request.port === 443) && request.path === "/" && request.method === "POST" && empty(request.query);
      }
      return next(args);
    }, { step: "finalizeRequest", priority: "low", name: "dexcostOcrRoute", override: true });
  } catch { /* Missing transport evidence never establishes successful usage. */ }
  const facade = new Proxy(client, { get(target, name) {
    const native = Reflect.get(target, name, target);
    if (typeof native !== "function") return native;
    if (name !== "send") return native.bind(target);
    return (...args: unknown[]) => {
      if (!state.active || (args[0] as Native)?.constructor?.name !== "DetectDocumentTextCommand" || args.some(arg => typeof arg === "function") || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
      const task = getCurrentTask(), started = new Date();
      const evidence = { attempts: 0, valid: false, host: `textract.${scope.region}.amazonaws.com` };
      return runWithProviderCapture("amazon_textract", async () => {
        let eligible = false;
        try {
          const config = (target as Native).config;
          const region = typeof config?.region === "function" ? await config.region() : config?.region;
          eligible = !!task && config?.serviceId === "Textract" && region === scope.region;
        } catch { /* Unknown client stays unpriced. */ }
        const result = await transport.run(evidence, () => Reflect.apply(native, target, args)) as Native;
        try {
          const meta = result?.$metadata, pages = result?.DocumentMetadata?.Pages, requestId = meta?.requestId;
          if (!eligible || !state.active || evidence.attempts !== 1 || !evidence.valid || !Number.isSafeInteger(pages) || pages <= 0 || meta?.httpStatusCode !== 200 || meta?.attempts !== 1 || typeof requestId !== "string" || !/^[A-Za-z0-9._-]{1,100}$/.test(requestId) || result.Error !== undefined) return result;
          const record = `${resource}/${requestId}`;
          if (tracker.buffer.getProviderJob("amazon_textract", "ocr", record) === undefined) tracker.buffer.insertProviderJobRevision(new ProviderJobRevision({
            taskId: task!.taskId, provider: "amazon_textract", service: "ocr", providerRecordId: record,
            operation: "ocr.detect_document_text", component: "external", eventType: "external_cost", resourceType: "endpoint", resourceId: resource,
            status: "succeeded", revision: 1, submittedAt: started, observedAt: new Date(),
            usage: [{ metric: "amazon_textract.detect_document_text_pages", quantity: new Decimal(pages), unit: "Pages" }],
          }));
        } catch { /* Telemetry cannot alter native values or expose document errors. */ }
        return result;
      });
    };
  } });
  states.set(facade, state); return facade;
}

export interface DocumentAIBinding { billingAccountId: string; processorVersion: string; processorType: "OCR_PROCESSOR" }
/** Official Google v1 promise API. Caller verifies OCR_PROCESSOR type/version;
 * the facade performs no GetProcessor lookup. Each call must disable retry with
 * {retry:null}; partial pages, field masks, premium/options and custom headers
 * are excluded. Returned pages are invoice allocation evidence, never cash.
 */
export function instrumentDocumentAI<T extends object>(client: T, tracker: CostTracker, options: DocumentAIBinding): T {
  const scope = { ...options };
  const match = /^projects\/([a-z0-9-]{1,30})\/locations\/([a-z0-9-]{2,30})\/processors\/([A-Za-z0-9_-]{1,32})\/processorVersions\/([A-Za-z0-9._-]{1,100})$/.exec(scope.processorVersion);
  if (scope.processorType !== "OCR_PROCESSOR" || !match) throw new Error("Document AI requires an explicit verified OCR_PROCESSOR version");
  const resource = databaseResourceId(scope.billingAccountId, `${match[1]}.${match[2]}.${match[3]}.enterprise_ocr`);
  const host = `${match[2]}-documentai.googleapis.com`, state = { active: true };
  const facade = new Proxy(client, { get(target, name) {
    const native = Reflect.get(target, name, target);
    if (typeof native !== "function") return native;
    if (name !== "processDocument") return native.bind(target);
    return (...args: unknown[]) => {
      if (!state.active || args.some(arg => typeof arg === "function") || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
      const task = getCurrentTask(), started = new Date(), request = args[0] as Native, call = args[1] as Native;
      let eligible = false;
      try {
        const source = request?.rawDocument ?? request?.gcsDocument;
        // The native apiEndpoint getter returns the default service, not the
        // configured regional host. Generated _opts.servicePath owns transport.
        eligible = !!task && (target as Native)._opts?.servicePath === host && call?.retry === null && ordinaryGoogleArgs(call?.otherArgs) &&
          request?.name === scope.processorVersion && empty(request.processOptions) && empty(request.fieldMask) &&
          empty(request.inlineDocument) && MIME.has(source?.mimeType);
      } catch { /* Unknown request options remain unobserved. */ }
      return runWithProviderCapture("google_document_ai", () => {
        const result = Reflect.apply(native, target, args);
        return result.then((response: unknown) => {
          try {
            const document = Array.isArray(response) ? response[0]?.document : undefined, pages = document?.pages;
            if (!eligible || !state.active || !Array.isArray(pages) || pages.length === 0 || !empty(document.shardInfo) || (document.error?.code ?? 0) !== 0 || pages.some((page: Native, i: number) => !Number.isSafeInteger(page?.pageNumber) || page.pageNumber !== i + 1)) return response;
            const ended = new Date();
            if (ended < started) return response;
            tracker.buffer.addEvent(createCostEvent({ eventId: randomUUID(), taskId: task!.taskId, occurredAt: ended,
              eventType: "external_cost", costConfidence: "unknown", provider: "google_document_ai", serviceName: "ocr",
              details: { attribution_component: "external", attribution_resource_type: "endpoint", attribution_resource_id: resource,
                attribution_operation_name: "ocr.process_document", attribution_operation_status: "succeeded",
                attribution_usage_duration_seconds: (ended.getTime() - started.getTime()) / 1000,
                attribution_usage_lines: [{ metric: "google_document_ai.enterprise_ocr_pages", quantity: String(pages.length), unit: "Pages" }],
                ocr_capture_basis: "provider_returned_pages_local_call_identity" },
            }));
          } catch { /* Only allowlisted counters persist; native response is unchanged. */ }
          return response;
        });
      });
    };
  } });
  states.set(facade, state); return facade;
}
export function uninstrumentOcr(client: object): void { const state = states.get(client); if (state) state.active = false; }
