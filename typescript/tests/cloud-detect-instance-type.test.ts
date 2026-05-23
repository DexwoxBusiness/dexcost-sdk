/**
 * CloudEnv carries instanceType extracted by Phase 2 IMDS probes (Decision #3).
 *
 * The compute pricing engine reads instanceType at task finalize to resolve
 * EC2 / GCE / Azure VM SKU rates. Per Decision #3 the instance-type fetch shares
 * the same Phase 2 background thread that already runs for the region probe —
 * one probe, two values extracted.
 *
 * Ports python/tests/test_cloud_detect_instance_type.py (8 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import {
  _probes,
  _setFetchForTests,
  _resetCloudDetectForTests,
  type CloudEnv,
} from "../src/cloud-detect.js";

beforeEach(() => {
  _resetCloudDetectForTests();
});

afterEach(() => {
  _setFetchForTests(null);
  _resetCloudDetectForTests();
});

function makeFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
): typeof fetch {
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    return Promise.resolve(handler(url, init));
  }) as typeof fetch;
}

describe("CloudEnv.instanceType", () => {
  test("CloudEnv carries instanceType field", () => {
    const env: CloudEnv = {
      provider: "aws",
      region: "us-east-1",
      source: "imds",
      instanceType: "c7g.xlarge",
    };
    expect(env.instanceType).toBe("c7g.xlarge");
  });

  test("CloudEnv.instanceType defaults to null", () => {
    const env: CloudEnv = { provider: null, region: null, source: "none" };
    expect(env.instanceType ?? null).toBeNull();
  });
});

describe("AWS IMDS instance-type extraction", () => {
  test("AWS probe returns instanceType", async () => {
    const calls: string[] = [];
    _setFetchForTests(
      makeFetch((url) => {
        calls.push(url);
        if (url.endsWith("/api/token")) return new Response("TOKEN");
        if (url.endsWith("/placement/region")) return new Response("us-east-1");
        if (url.endsWith("/meta-data/instance-type")) return new Response("c7g.xlarge");
        return new Response("", { status: 404 });
      }),
    );

    const env = await _probes.aws();
    expect(env).not.toBeNull();
    expect(env!.provider).toBe("aws");
    expect(env!.region).toBe("us-east-1");
    expect(env!.instanceType).toBe("c7g.xlarge");
    expect(calls.some((u) => u.includes("/meta-data/instance-type"))).toBe(true);
  });

  test("AWS probe instance-type failure does not lose region", async () => {
    _setFetchForTests(
      makeFetch((url) => {
        if (url.endsWith("/api/token")) return new Response("TOKEN");
        if (url.endsWith("/placement/region")) return new Response("eu-west-2");
        if (url.endsWith("/meta-data/instance-type")) {
          throw new Error("simulated instance-type 404");
        }
        return new Response("", { status: 404 });
      }),
    );

    const env = await _probes.aws();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("eu-west-2");
    expect(env!.instanceType ?? null).toBeNull();
  });
});

describe("GCP IMDS machine-type extraction", () => {
  test("GCP probe returns machine-type", async () => {
    _setFetchForTests(
      makeFetch((url) => {
        if (url.endsWith("/instance/region"))
          return new Response("projects/123/regions/us-central1");
        if (url.endsWith("/instance/machine-type"))
          return new Response("projects/123/machineTypes/n2-standard-2");
        return new Response("", { status: 404 });
      }),
    );

    const env = await _probes.gcp();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("us-central1");
    expect(env!.instanceType).toBe("n2-standard-2");
  });

  test("GCP probe machine-type failure does not lose region", async () => {
    _setFetchForTests(
      makeFetch((url) => {
        if (url.endsWith("/instance/region"))
          return new Response("projects/123/regions/us-central1");
        if (url.endsWith("/instance/machine-type")) {
          throw new Error("simulated 404");
        }
        return new Response("", { status: 404 });
      }),
    );

    const env = await _probes.gcp();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("us-central1");
    expect(env!.instanceType ?? null).toBeNull();
  });
});

describe("Azure IMDS vmSize extraction", () => {
  test("Azure probe returns vmSize", async () => {
    const payload = JSON.stringify({
      compute: { location: "eastus", vmSize: "Standard_D2s_v3" },
    });
    _setFetchForTests(
      makeFetch(() => new Response(payload, { headers: { "Content-Type": "application/json" } })),
    );

    const env = await _probes.azure();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("eastus");
    expect(env!.instanceType).toBe("Standard_D2s_v3");
  });

  test("Azure probe missing vmSize returns null instanceType", async () => {
    const payload = JSON.stringify({ compute: { location: "eastus" } });
    _setFetchForTests(
      makeFetch(() => new Response(payload, { headers: { "Content-Type": "application/json" } })),
    );

    const env = await _probes.azure();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("eastus");
    expect(env!.instanceType ?? null).toBeNull();
  });
});
