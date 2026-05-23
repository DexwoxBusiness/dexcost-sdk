/**
 * Fargate ECS task metadata helper.
 *
 * Single HTTP call per process, cached. Exposes vcpuCount (number) and
 * memoryBytesLimit (number — converted from MiB per Decision #7; Fargate
 * uses BINARY MiB, not decimal MB, which is the ~4.86% silent-over-
 * attribution bug the conversion table prevents).
 *
 * Ports python/tests/test_fargate_metadata.py (6 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  fetchFargateMetadata,
  _resetForTests,
  _setFetchForTests,
} from "../src/core/fargate-metadata.js";

let snapshot: Record<string, string | undefined> = {};

beforeEach(() => {
  _resetForTests();
  snapshot = {
    ECS_CONTAINER_METADATA_URI_V4: process.env.ECS_CONTAINER_METADATA_URI_V4,
    ECS_CONTAINER_METADATA_URI: process.env.ECS_CONTAINER_METADATA_URI,
  };
  delete process.env.ECS_CONTAINER_METADATA_URI_V4;
  delete process.env.ECS_CONTAINER_METADATA_URI;
});

afterEach(() => {
  _setFetchForTests(null);
  _resetForTests();
  for (const [k, v] of Object.entries(snapshot)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

function makeFetch(
  handler: (url: string) => Response | Promise<Response>,
): typeof fetch {
  return ((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    return Promise.resolve(handler(url));
  }) as typeof fetch;
}

describe("fetchFargateMetadata", () => {
  test("returns vcpu and memory (MiB → bytes)", async () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/abc";
    _setFetchForTests(
      makeFetch(
        () =>
          new Response(
            JSON.stringify({
              TaskARN: "arn:aws:ecs:us-east-1:0:task/abc",
              Limits: { CPU: 0.5, Memory: 1024 },
            }),
          ),
      ),
    );

    const m = await fetchFargateMetadata();
    expect(m).not.toBeNull();
    expect(m!.vcpuCount).toBe(0.5);
    // 1024 MiB → bytes via binary GiB (Decision #7)
    expect(m!.memoryBytesLimit).toBe(1024 * 1024 * 1024);
  });

  test("no env var returns null", async () => {
    expect(await fetchFargateMetadata()).toBeNull();
  });

  test("unreachable returns null and logs once", async () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/abc";
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    _setFetchForTests(
      makeFetch(() => {
        throw new Error("network unreachable");
      }),
    );

    expect(await fetchFargateMetadata()).toBeNull();
    expect(await fetchFargateMetadata()).toBeNull();

    const fargateLogs = warn.mock.calls.filter((args) =>
      args.some(
        (a) => typeof a === "string" && a.toLowerCase().includes("fargate metadata"),
      ),
    );
    expect(fargateLogs.length).toBe(1);
    warn.mockRestore();
  });

  test("cached after first success", async () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/abc";
    let calls = 0;
    _setFetchForTests(
      makeFetch(() => {
        calls += 1;
        return new Response(JSON.stringify({ Limits: { CPU: 1, Memory: 512 } }));
      }),
    );

    const a = await fetchFargateMetadata();
    const b = await fetchFargateMetadata();
    expect(a).not.toBeNull();
    expect(a).toBe(b);
    expect(calls).toBe(1);
  });

  test("malformed Limits returns null", async () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/abc";
    _setFetchForTests(
      makeFetch(
        () =>
          new Response(JSON.stringify({ Limits: { CPU: "garbage" } })),
      ),
    );
    expect(await fetchFargateMetadata()).toBeNull();
  });

  test("v3 URI (ECS_CONTAINER_METADATA_URI, no _V4 suffix) is also valid", async () => {
    process.env.ECS_CONTAINER_METADATA_URI = "http://169.254.170.2/v3/abc";
    _setFetchForTests(
      makeFetch(
        () =>
          new Response(JSON.stringify({ Limits: { CPU: 2, Memory: 4096 } })),
      ),
    );
    const m = await fetchFargateMetadata();
    expect(m).not.toBeNull();
    expect(m!.vcpuCount).toBe(2);
    expect(m!.memoryBytesLimit).toBe(4096 * 1024 * 1024);
  });
});
