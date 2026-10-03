/** Successful S3-compatible request evidence. Reconciled invoices alone own money.
 * No object keys/content, transfer estimates, byte-months or SDK price formulas.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { getCurrentTask } from "../core/context.js";
import { Decimal } from "../core/models.js";
import { ProviderJobRevision } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

export interface ObjectStorageBinding {
  provider: "aws_s3" | "r2_cloudflare";
  billingAccountId: string;
  bucket: string;
  region: string;
  bucketOwnerAccountId?: string;
  ownerPays?: boolean;
}
type Native = Record<string, any>;
const commands: Record<string, [string, string]> = {
  GetObjectCommand: ["get_object", "get_requests"], PutObjectCommand: ["put_object", "put_requests"],
  ListObjectsV2Command: ["list_objects_v2", "list_requests"],
};
const states = new WeakMap<object, { active: boolean }>();
type TransportEvidence = { attempts: number; valid: boolean; hosts: string[]; bucket: string; method: string };
const transport = new AsyncLocalStorage<TransportEvidence>();
function observeRequest(request: Native): void {
  const evidence = transport.getStore();
  if (!evidence) return;
  evidence.attempts++;
  try {
    const routed = (evidence.hosts.includes(request.hostname) && (request.path === `/${evidence.bucket}` || request.path?.startsWith(`/${evidence.bucket}/`))) || evidence.hosts.some(host => request.hostname === `${evidence.bucket}.${host}`);
    evidence.valid = request.protocol === "https:" && (request.port === undefined || request.port === 443) && routed && request.method === evidence.method;
  } catch { evidence.valid = false; }
}
function validBucket(value: string): boolean {
  return /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(value) && !value.includes("..") && !/(?:--x-s3|-s3alias|\.mrap)$/.test(value);
}
async function route(client: Native, scope: ObjectStorageBinding): Promise<boolean> {
  const config = client.config;
  if (config?.serviceId !== "S3" || config.useAccelerateEndpoint || config.useDualstackEndpoint === true || config.useFipsEndpoint === true) return false;
  const region = typeof config.region === "function" ? await config.region() : config.region;
  if (region !== scope.region) return false;
  if (config.endpoint === undefined) return scope.provider === "aws_s3";
  const endpoint = typeof config.endpoint === "function" ? await config.endpoint() : config.endpoint;
  const raw = endpoint?.url ?? endpoint;
  const url = typeof raw === "string" || raw instanceof URL ? new URL(raw) : new URL(`${raw?.protocol}//${raw?.hostname}${raw?.port ? ':' + raw.port : ''}${raw?.path ?? '/'}`);
  const allowed = scope.provider === "aws_s3" ? [`s3.${scope.region}.amazonaws.com`, ...(scope.region === "us-east-1" ? ["s3.amazonaws.com"] : [])] : [`${scope.billingAccountId}.r2.cloudflarestorage.com`];
  return url.protocol === "https:" && allowed.includes(url.hostname) && !url.port && !url.username && !url.password && !url.search && !url.hash && url.pathname === "/";
}
/** AWS SDK v3 send facade. Bind each bucket explicitly; R2 invoice resource is
 * the account endpoint, not an invented bucket subtotal. A transparent middleware
 * observes final transport routing; it never changes or sends a request.
 */
export function instrumentObjectStorage<T extends object>(client: T, tracker: CostTracker, options: ObjectStorageBinding): T {
  const scope = { ...options };
  if (!validBucket(scope.bucket)) throw new Error("An explicit general-purpose bucket name is required");
  if (scope.provider === "aws_s3") {
    if (!/^[0-9]{12}$/.test(scope.billingAccountId) || !/^[0-9]{12}$/.test(scope.bucketOwnerAccountId ?? "") || !/^[a-z]{2}(?:-[a-z]+)+-[0-9]$/.test(scope.region) || scope.ownerPays !== true) throw new Error("AWS requires payer, bucket-owner, region and ownerPays=true");
  } else if (scope.provider === "r2_cloudflare") {
    if (!/^[a-f0-9]{32}$/.test(scope.billingAccountId) || scope.region !== "auto" || scope.bucketOwnerAccountId !== undefined) throw new Error("R2 requires account ID, region auto and no AWS owner mapping");
  } else throw new Error("Unsupported object-storage provider");
  const resource = databaseResourceId(scope.billingAccountId, scope.provider === "aws_s3" ? `${scope.bucketOwnerAccountId}.${scope.region}.${scope.bucket}` : "r2");
  const state = { active: true };
  try {
    (client as Native).middlewareStack.add((next: (args: Native) => Promise<unknown>) => (args: Native) => {
      observeRequest(args.request); return next(args);
    }, { step: "finalizeRequest", priority: "low", name: "dexcostObjectStorageRoute", override: true });
  } catch { /* Missing actual transport evidence leaves capture disabled. */ }
  const facade = new Proxy(client, { get(target, name) {
    const native = Reflect.get(target, name, target);
    if (typeof native !== "function") return native;
    if (name !== "send") return native.bind(target);
    return (...args: unknown[]) => {
      const command = args[0] as Native | undefined, op = commands[command?.constructor?.name ?? ""];
      if (!state.active || !op || args.some(arg => typeof arg === "function") || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
      const task = getCurrentTask(), started = new Date(), input = command?.input;
      // Copy allowlisted billing facts before the asynchronous call; never keys/body.
      const bucket = input?.Bucket, storageClass = input?.StorageClass, payer = input?.RequestPayer, expectedOwner = input?.ExpectedBucketOwner;
      return runWithProviderCapture(scope.provider, async () => {
        let eligible = false;
        try { eligible = !!task && bucket === scope.bucket && payer === undefined && (expectedOwner === undefined || expectedOwner === scope.bucketOwnerAccountId) && await route(target, scope); } catch { /* Unknown route stays unobserved. */ }
        const hosts = scope.provider === "aws_s3" ? [`s3.${scope.region}.amazonaws.com`, ...(scope.region === "us-east-1" ? ["s3.amazonaws.com"] : [])] : [`${scope.billingAccountId}.r2.cloudflarestorage.com`];
        const evidence: TransportEvidence = { attempts: 0, valid: false, hosts, bucket: scope.bucket, method: op[0] === "put_object" ? "PUT" : "GET" };
        const result = await transport.run(evidence, () => Reflect.apply(native, target, args));
        try {
          const metadata = result?.$metadata, requestId = metadata?.requestId;
          if (!eligible || evidence.attempts !== 1 || !evidence.valid || !state.active || !result || result.RequestCharged !== undefined || result.Error !== undefined || typeof requestId !== "string" || !/^[A-Za-z0-9._-]{1,100}$/.test(requestId) || !Number.isInteger(metadata.httpStatusCode) || metadata.httpStatusCode < 200 || metadata.httpStatusCode >= 300 || metadata.attempts !== 1) return result;
          const classValue = op[0] === "get_object" ? result.StorageClass : storageClass;
          let metric: string;
          if (scope.provider === "aws_s3") {
            if (op[0] !== "list_objects_v2" && classValue !== undefined && classValue !== "STANDARD") return result;
            metric = `aws_s3.${op[1]}`;
          } else {
            if (op[0] === "list_objects_v2" || !["STANDARD", "STANDARD_IA"].includes(classValue)) return result;
            metric = `r2_cloudflare.${classValue === "STANDARD" ? "standard" : "ia"}_class_${op[0] === "put_object" ? "a" : "b"}`;
          }
          const record = `${resource}/${requestId}`;
          if (tracker.buffer.getProviderJob(scope.provider, "object_storage", record)) return result;
          tracker.buffer.insertProviderJobRevision(new ProviderJobRevision({ taskId: task!.taskId,
            provider: scope.provider, service: "object_storage", providerRecordId: record, operation: `object_storage.${op[0]}`,
            component: "storage", eventType: "external_cost", resourceType: "endpoint", resourceId: resource,
            status: "succeeded", submittedAt: started, observedAt: new Date(),
            usage: [{ metric, quantity: new Decimal(1), unit: "Requests" }],
          }));
        } catch { /* Telemetry cannot alter native provider behavior. */ }
        return result;
      });
    };
  } });
  states.set(facade, state);
  return facade;
}
export function uninstrumentObjectStorage(client: object): void {
  const state = states.get(client); if (state) state.active = false;
}
