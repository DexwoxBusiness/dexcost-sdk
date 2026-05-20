/**
 * Active compute-runtime resolver.
 *
 * Cascade priority (capture spec §5.5):
 *
 *   1. Serverless env vars — Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
 *      Azure Functions, Vercel
 *   2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over the underlying VM so a
 *      pod-on-EC2 is billed once as k8s_pod, not twice as k8s_pod + ec2)
 *   3. cloud_detect IaaS fallback — EC2 / GCE / Azure VM via the existing
 *      CloudEnv.provider resolved by cloud-detect.
 *   4. UNKNOWN
 *
 * The discriminator value emitted on compute_cost events
 * (details.billing_model) is derived from this enum in
 * compute-accountant._billingModelFor.
 *
 * **String values match Python** — cross-SDK event portability. Do not change.
 *
 * Mirrors python/src/dexcost/compute_runtime.py.
 */

import { getCloudEnv } from "../cloud-detect.js";

/**
 * Runtime kind discriminator. String values are pinned to Python so events
 * serialized in one SDK can be consumed by another.
 */
export const RuntimeKind = {
  Lambda: "lambda",
  Fargate: "fargate",
  Ec2: "ec2",
  CloudRun: "cloud_run",
  CloudFunctions: "cloud_functions",
  Gce: "gce",
  AzureFunctions: "azure_functions",
  AzureVm: "azure_vm",
  Vercel: "vercel_fluid",
  K8sPod: "k8s_pod",
  Unknown: "unknown",
} as const;

export type RuntimeKind = (typeof RuntimeKind)[keyof typeof RuntimeKind];

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

/** Return the active compute runtime for the current process. */
export function resolveRuntime(): RuntimeKind {
  // In browser bundles process.env doesn't expose the serverless env vars
  // — collapse straight to Unknown.
  if (!_isNode()) return RuntimeKind.Unknown;

  const env = process.env;

  // 1. Serverless env vars — highest priority.
  if (env.AWS_LAMBDA_FUNCTION_NAME) return RuntimeKind.Lambda;
  if (env.ECS_CONTAINER_METADATA_URI_V4 || env.ECS_CONTAINER_METADATA_URI) {
    return RuntimeKind.Fargate;
  }
  if (env.K_SERVICE) {
    // Cloud Functions Gen2 sets BOTH K_SERVICE and FUNCTION_TARGET; plain
    // Cloud Run sets only K_SERVICE.
    if (env.FUNCTION_TARGET) return RuntimeKind.CloudFunctions;
    return RuntimeKind.CloudRun;
  }
  if (env.FUNCTIONS_WORKER_RUNTIME) return RuntimeKind.AzureFunctions;
  if (env.VERCEL) return RuntimeKind.Vercel;

  // 2. Kubernetes wins over the underlying VM.
  if (env.KUBERNETES_SERVICE_HOST) return RuntimeKind.K8sPod;

  // 3. Fall through to cloud-detect IaaS classification.
  const cloud = getCloudEnv();
  if (cloud.provider === "aws") return RuntimeKind.Ec2;
  if (cloud.provider === "gcp") return RuntimeKind.Gce;
  if (cloud.provider === "azure") return RuntimeKind.AzureVm;

  return RuntimeKind.Unknown;
}
