/**
 * Compute runtime resolution — env-var cascade + cloud_detect fallback.
 *
 * Per capture spec §5.5:
 *   1. Serverless env vars (Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
 *      Azure Functions, Vercel)
 *   2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over the underlying VM to
 *      avoid double-counting)
 *   3. cloud_detect IaaS (EC2 / GCE / Azure VM)
 *   4. UNKNOWN
 *
 * Ports python/tests/test_compute_runtime.py (12 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { RuntimeKind, resolveRuntime } from "../src/core/compute-runtime.js";
import { _resetCloudDetectForTests, _setResultForTests } from "../src/cloud-detect.js";

const SERVERLESS_ENV_VARS = [
  "AWS_LAMBDA_FUNCTION_NAME",
  "ECS_CONTAINER_METADATA_URI_V4",
  "ECS_CONTAINER_METADATA_URI",
  "K_SERVICE",
  "FUNCTION_TARGET",
  "FUNCTIONS_WORKER_RUNTIME",
  "VERCEL",
  "KUBERNETES_SERVICE_HOST",
];

let snapshot: Record<string, string | undefined> = {};

beforeEach(() => {
  snapshot = {};
  for (const k of SERVERLESS_ENV_VARS) {
    snapshot[k] = process.env[k];
    delete process.env[k];
  }
  _resetCloudDetectForTests();
});

afterEach(() => {
  for (const [k, v] of Object.entries(snapshot)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  _resetCloudDetectForTests();
});

describe("resolveRuntime", () => {
  test("Lambda env wins", () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "fn";
    expect(resolveRuntime()).toBe(RuntimeKind.Lambda);
  });

  test("Fargate env wins", () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/abc";
    expect(resolveRuntime()).toBe(RuntimeKind.Fargate);
  });

  test("Cloud Run env wins", () => {
    process.env.K_SERVICE = "svc";
    expect(resolveRuntime()).toBe(RuntimeKind.CloudRun);
  });

  test("Cloud Functions Gen2 disambiguated from Cloud Run", () => {
    process.env.K_SERVICE = "svc";
    process.env.FUNCTION_TARGET = "main";
    expect(resolveRuntime()).toBe(RuntimeKind.CloudFunctions);
  });

  test("Azure Functions env wins", () => {
    process.env.FUNCTIONS_WORKER_RUNTIME = "node";
    expect(resolveRuntime()).toBe(RuntimeKind.AzureFunctions);
  });

  test("Vercel env wins", () => {
    process.env.VERCEL = "1";
    expect(resolveRuntime()).toBe(RuntimeKind.Vercel);
  });

  test("k8s wins over aws IaaS — pod on EC2 must NOT double-count", () => {
    process.env.KUBERNETES_SERVICE_HOST = "10.0.0.1";
    _setResultForTests({
      provider: "aws",
      region: "us-east-1",
      source: "dmi",
      instanceType: "c7g.xlarge",
    });
    expect(resolveRuntime()).toBe(RuntimeKind.K8sPod);
  });

  test("falls through to cloud_detect EC2", () => {
    _setResultForTests({
      provider: "aws",
      region: "us-east-1",
      source: "dmi",
      instanceType: "c7g.xlarge",
    });
    expect(resolveRuntime()).toBe(RuntimeKind.Ec2);
  });

  test("falls through to cloud_detect GCE", () => {
    _setResultForTests({
      provider: "gcp",
      region: "us-central1",
      source: "imds",
      instanceType: "n2-standard-2",
    });
    expect(resolveRuntime()).toBe(RuntimeKind.Gce);
  });

  test("falls through to cloud_detect Azure VM", () => {
    _setResultForTests({
      provider: "azure",
      region: "eastus",
      source: "imds",
      instanceType: "Standard_D2s_v3",
    });
    expect(resolveRuntime()).toBe(RuntimeKind.AzureVm);
  });

  test("undetected returns Unknown", () => {
    _setResultForTests({ provider: null, region: null, source: "none" });
    expect(resolveRuntime()).toBe(RuntimeKind.Unknown);
  });

  test("serverless wins over IaaS — Lambda always reports as LAMBDA", () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "fn";
    _setResultForTests({
      provider: "aws",
      region: "us-east-1",
      source: "dmi",
      instanceType: "c7g.xlarge",
    });
    expect(resolveRuntime()).toBe(RuntimeKind.Lambda);
  });
});
