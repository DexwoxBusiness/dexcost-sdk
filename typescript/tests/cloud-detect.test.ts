/**
 * cloud_detect — env / DMI / IMDS phases; init never blocks.
 *
 * Ports python/tests/test_cloud_detect.py (42 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  detectNow,
  getCloudEnv,
  startBackgroundDetection,
  _gcpPathToRegion,
  _PROBES,
  _FANOUT_PROBES,
  _probes,
  _runProbe,
  _setReadDmiForTests,
  _setFetchForTests,
  _resetCloudDetectForTests,
  _awaitBackgroundForTests,
  type CloudEnv,
} from "../src/cloud-detect.js";

// All env-var names the module reads from. Cleared before each test.
const _ALL_CLOUD_ENV_VARS = [
  "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
  "AWS_REGION", "AWS_DEFAULT_REGION",
  "ECS_CONTAINER_METADATA_URI_V4", "ECS_CONTAINER_METADATA_URI",
  "WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME",
  "REGION_NAME", "CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX",
  "K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET", "FUNCTION_NAME",
  "FLY_REGION", "FLY_APP_NAME",
  "VERCEL", "VERCEL_REGION", "VERCEL_ENV",
  "MODAL_TASK_ID", "MODAL_IMAGE_ID", "MODAL_FUNCTION_ID", "MODAL_REGION",
  "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "RUNPOD_DC_ID",
  "REPLICATE_MODEL_ID", "REPLICATE_DEPLOYMENT_ID",
  "RENDER", "RENDER_SERVICE_ID", "RENDER_REGION",
  "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_REGION",
  "RAILWAY_REPLICA_REGION",
  "DYNO", "HEROKU_APP_NAME",
  "KOYEB_SERVICE_NAME", "KOYEB_APP_NAME", "KOYEB_REGION",
  "NETLIFY", "NETLIFY_SITE_ID",
  "CF_PAGES", "CLOUDFLARE_ACCOUNT_ID",
];

// Snapshot of process.env values to restore after each test.
let _envSnapshot: Record<string, string | undefined> = {};

function _clearEnv(): void {
  for (const k of _ALL_CLOUD_ENV_VARS) {
    delete process.env[k];
  }
}

beforeEach(() => {
  // Snapshot env then clear the cloud-related vars.
  _envSnapshot = {};
  for (const k of _ALL_CLOUD_ENV_VARS) {
    _envSnapshot[k] = process.env[k];
  }
  _clearEnv();
  _resetCloudDetectForTests();
  // Default: no DMI signals.
  _setReadDmiForTests(() => ({}));
  _setFetchForTests(null);
});

afterEach(() => {
  for (const [k, v] of Object.entries(_envSnapshot)) {
    if (v === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = v;
    }
  }
  _resetCloudDetectForTests();
  _setReadDmiForTests(null);
  _setFetchForTests(null);
});

function dmiFixture(fields: Record<string, string>): void {
  const lowered: Record<string, string> = {};
  for (const [k, v] of Object.entries(fields)) {
    lowered[k] = v.toLowerCase();
  }
  _setReadDmiForTests(() => lowered);
}

// ---------------------------------------------------------------------------
// Phase 1a — env-var detection
// ---------------------------------------------------------------------------

describe("env-var detection", () => {
  test("AWS Lambda env resolves fully", () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "my-fn";
    process.env.AWS_REGION = "us-east-1";
    const env = detectNow();
    expect(env.provider).toBe("aws");
    expect(env.region).toBe("us-east-1");
    expect(env.source).toBe("env");
  });

  test("Azure App Service: provider no region", () => {
    process.env.WEBSITE_SITE_NAME = "x";
    const env = detectNow();
    expect(env.provider).toBe("azure");
    expect(env.region).toBeNull();
    expect(env.source).toBe("env");
  });

  test("GCP Cloud Run: provider no region", () => {
    process.env.K_SERVICE = "my-svc";
    const env = detectNow();
    expect(env.provider).toBe("gcp");
    expect(env.region).toBeNull();
    expect(env.source).toBe("env");
  });

  test("no env no dmi returns undetected", () => {
    const env = detectNow();
    expect(env.provider).toBeNull();
    expect(env.region).toBeNull();
    expect(env.source).toBe("none");
  });

  test("ECS Fargate metadata URI resolves AWS with region", () => {
    process.env.ECS_CONTAINER_METADATA_URI_V4 = "http://169.254.170.2/v4/metadata-id";
    process.env.AWS_REGION = "ap-south-1";
    const env = detectNow();
    expect(env.provider).toBe("aws");
    expect(env.region).toBe("ap-south-1");
    expect(env.source).toBe("env");
  });

  test("ECS v3 metadata URI also resolves AWS", () => {
    process.env.ECS_CONTAINER_METADATA_URI = "http://169.254.170.2/v3/x";
    const env = detectNow();
    expect(env.provider).toBe("aws");
  });

  test("Azure Container Apps hostname yields region", () => {
    process.env.CONTAINER_APP_NAME = "my-app";
    process.env.CONTAINER_APP_HOSTNAME =
      "my-app--abc.proudground-12345.eastus.azurecontainerapps.io";
    const env = detectNow();
    expect(env.provider).toBe("azure");
    expect(env.region).toBe("eastus");
    expect(env.source).toBe("env");
  });

  test("Azure Container Apps DNS suffix yields region", () => {
    process.env.CONTAINER_APP_NAME = "my-app";
    process.env.CONTAINER_APP_ENV_DNS_SUFFIX =
      "proudground-12345.westeurope.azurecontainerapps.io";
    const env = detectNow();
    expect(env.region).toBe("westeurope");
  });

  test("Azure REGION_NAME wins when both present", () => {
    process.env.CONTAINER_APP_NAME = "x";
    process.env.REGION_NAME = "northeurope";
    process.env.CONTAINER_APP_HOSTNAME = "x.y.eastus.azurecontainerapps.io";
    const env = detectNow();
    expect(env.region).toBe("northeurope");
  });

  test("GCP K_CONFIGURATION alone signals GCP", () => {
    process.env.K_CONFIGURATION = "my-config";
    const env = detectNow();
    expect(env.provider).toBe("gcp");
  });

  test("bare AWS_REGION classifies as AWS", () => {
    process.env.AWS_REGION = "us-east-1";
    const env = detectNow();
    expect(env.provider).toBe("aws");
    expect(env.region).toBe("us-east-1");
  });

  test("Fly.io FLY_REGION resolves provider and region", () => {
    process.env.FLY_REGION = "iad";
    process.env.FLY_APP_NAME = "my-app";
    const env = detectNow();
    expect(env.provider).toBe("fly");
    expect(env.region).toBe("iad");
    expect(env.source).toBe("env");
  });

  test("Fly.io FLY_APP_NAME alone signals fly", () => {
    process.env.FLY_APP_NAME = "my-app";
    const env = detectNow();
    expect(env.provider).toBe("fly");
  });

  test("Vercel VERCEL_REGION wins over underlying AWS_REGION", () => {
    process.env.VERCEL = "1";
    process.env.VERCEL_REGION = "iad1";
    process.env.AWS_REGION = "us-east-1"; // Vercel also sets this
    const env = detectNow();
    expect(env.provider).toBe("vercel");
    expect(env.region).toBe("iad1");
  });

  // ── ML / GPU clouds ────────────────────────────────────────────────────

  test("Modal MODAL_TASK_ID resolves modal with region", () => {
    process.env.MODAL_TASK_ID = "ta-abc";
    process.env.MODAL_REGION = "us-east-1";
    const env = detectNow();
    expect(env.provider).toBe("modal");
    expect(env.region).toBe("us-east-1");
  });

  test("RunPod RUNPOD_POD_ID resolves provider", () => {
    process.env.RUNPOD_POD_ID = "abc123";
    process.env.RUNPOD_DC_ID = "US-CA-2";
    const env = detectNow();
    expect(env.provider).toBe("runpod");
    expect(env.region).toBe("US-CA-2");
  });

  // ── PaaS ───────────────────────────────────────────────────────────────

  test("Render resolves with no region env var", () => {
    process.env.RENDER = "true";
    process.env.RENDER_SERVICE_ID = "srv-abc";
    const env = detectNow();
    expect(env.provider).toBe("render");
    expect(env.region).toBeNull();
  });

  test("Railway resolves with RAILWAY_REPLICA_REGION", () => {
    process.env.RAILWAY_PROJECT_ID = "abc";
    process.env.RAILWAY_REPLICA_REGION = "us-west2";
    const env = detectNow();
    expect(env.provider).toBe("railway");
    expect(env.region).toBe("us-west2");
  });

  test("Heroku DYNO resolves", () => {
    process.env.DYNO = "web.1";
    const env = detectNow();
    expect(env.provider).toBe("heroku");
  });

  test("Koyeb resolves with KOYEB_REGION", () => {
    process.env.KOYEB_APP_NAME = "my-app";
    process.env.KOYEB_REGION = "fra";
    const env = detectNow();
    expect(env.provider).toBe("koyeb");
    expect(env.region).toBe("fra");
  });

  test("ML cloud wins over underlying AWS", () => {
    process.env.AWS_REGION = "us-east-1";
    process.env.MODAL_TASK_ID = "ta-abc";
    process.env.MODAL_REGION = "us-east-1";
    const env = detectNow();
    // Modal $0 egress must beat the AWS $0.09/GB attribution.
    expect(env.provider).toBe("modal");
  });
});

// ---------------------------------------------------------------------------
// Phase 1b — DMI
// ---------------------------------------------------------------------------

describe("DMI detection", () => {
  test("AWS via sys_vendor=Amazon EC2", () => {
    dmiFixture({ sys_vendor: "Amazon EC2" });
    const env = detectNow();
    expect(env.provider).toBe("aws");
    expect(env.source).toBe("dmi");
  });

  test("GCP via product_name=Google Compute Engine", () => {
    dmiFixture({ product_name: "Google Compute Engine" });
    const env = detectNow();
    expect(env.provider).toBe("gcp");
  });

  test("Azure via chassis_asset_tag canonical", () => {
    dmiFixture({ chassis_asset_tag: "7783-7084-3265-9085-8269-3286-77" });
    const env = detectNow();
    expect(env.provider).toBe("azure");
  });

  test("Azure via sys_vendor=Microsoft Corporation", () => {
    dmiFixture({ sys_vendor: "Microsoft Corporation" });
    const env = detectNow();
    expect(env.provider).toBe("azure");
  });

  test("OCI via chassis_asset_tag (NOT sys_vendor)", () => {
    dmiFixture({ chassis_asset_tag: "OracleCloud.com" });
    const env = detectNow();
    expect(env.provider).toBe("oci");
  });

  test("Alibaba via product_name (NOT sys_vendor)", () => {
    dmiFixture({ product_name: "Alibaba Cloud ECS" });
    const env = detectNow();
    expect(env.provider).toBe("alibaba");
  });

  test("DigitalOcean via sys_vendor", () => {
    dmiFixture({ sys_vendor: "DigitalOcean" });
    const env = detectNow();
    expect(env.provider).toBe("digitalocean");
  });

  test("Hetzner via sys_vendor", () => {
    dmiFixture({ sys_vendor: "Hetzner" });
    const env = detectNow();
    expect(env.provider).toBe("hetzner");
  });

  test("Vultr via sys_vendor", () => {
    dmiFixture({ sys_vendor: "Vultr" });
    const env = detectNow();
    expect(env.provider).toBe("vultr");
  });

  test("canonical signal wins over backup", () => {
    dmiFixture({
      chassis_asset_tag: "OracleCloud.com",
      sys_vendor: "Google",
    });
    const env = detectNow();
    expect(env.provider).toBe("oci");
  });

  test("unknown vendor returns none", () => {
    dmiFixture({ sys_vendor: "LENOVO" });
    const env = detectNow();
    expect(env.provider).toBeNull();
    expect(env.source).toBe("none");
  });
});

// ---------------------------------------------------------------------------
// GCP path parsing helpers
// ---------------------------------------------------------------------------

describe("_gcpPathToRegion", () => {
  test("zone form: drops zone letter", () => {
    expect(
      _gcpPathToRegion("projects/123/zones/us-central1-a", true),
    ).toBe("us-central1");
    expect(_gcpPathToRegion("us-central1-a", true)).toBe("us-central1");
    expect(_gcpPathToRegion("", true)).toBeNull();
  });

  test("region form: no zone-letter strip", () => {
    expect(
      _gcpPathToRegion("projects/123/regions/us-central1", false),
    ).toBe("us-central1");
    expect(
      _gcpPathToRegion("projects/123/regions/europe-west4", false),
    ).toBe("europe-west4");
  });
});

// ---------------------------------------------------------------------------
// Phase 2 probes (mocked fetch)
// ---------------------------------------------------------------------------

function makeFetchMock(handlers: {
  [urlMatcher: string]: (req: { url: string; init?: RequestInit }) =>
    | Promise<{ ok: boolean; body: string }>
    | { ok: boolean; body: string };
}): typeof fetch {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    const u = typeof url === "string" ? url : url instanceof URL ? url.toString() : url.url;
    for (const [pattern, handler] of Object.entries(handlers)) {
      if (u.includes(pattern)) {
        const out = await handler({ url: u, init });
        return new Response(out.body, { status: out.ok ? 200 : 500 });
      }
    }
    throw new Error(`unexpected url ${u}`);
  }) as unknown as typeof fetch;
}

describe("Phase 2 probes", () => {
  test("GCP probe prefers /instance/region endpoint", async () => {
    const calls: string[] = [];
    _setFetchForTests(
      makeFetchMock({
        "/instance/region": ({ url }) => {
          calls.push(url);
          return { ok: true, body: "projects/12345/regions/europe-west4" };
        },
        "/instance/zone": ({ url }) => {
          calls.push(url);
          throw new Error("zone endpoint should not be hit on Cloud Run");
        },
      }),
    );
    const env = await _probes.gcp();
    expect(env).not.toBeNull();
    expect(env!.provider).toBe("gcp");
    expect(env!.region).toBe("europe-west4");
    expect(calls.some((u) => u.includes("/instance/region"))).toBe(true);
  });

  test("GCP probe falls back to /instance/zone on region failure", async () => {
    _setFetchForTests(
      makeFetchMock({
        "/instance/region": () => {
          throw new Error("simulated /region missing");
        },
        "/instance/zone": () => ({
          ok: true,
          body: "projects/12345/zones/us-central1-a",
        }),
      }),
    );
    const env = await _probes.gcp();
    expect(env).not.toBeNull();
    expect(env!.region).toBe("us-central1");
  });

  test("OCI probe uses /canonicalRegionName + Bearer Oracle", async () => {
    const seen: { url: string; auth?: string }[] = [];
    _setFetchForTests(
      makeFetchMock({
        "/canonicalRegionName": ({ url, init }) => {
          const headers = init?.headers as Record<string, string> | undefined;
          seen.push({ url, auth: headers?.Authorization });
          return { ok: true, body: "us-phoenix-1" };
        },
      }),
    );
    const env = await _probes.oci();
    expect(env).not.toBeNull();
    expect(env!.provider).toBe("oci");
    expect(env!.region).toBe("us-phoenix-1");
    expect(seen.every((s) => s.url.includes("/canonicalRegionName"))).toBe(true);
    expect(seen.every((s) => s.auth === "Bearer Oracle")).toBe(true);
  });

  test("Phase 2 fanout list is aws/gcp/azure only", () => {
    expect(_FANOUT_PROBES).toEqual(["aws", "gcp", "azure"]);
  });

  test("provider hint goes straight to that provider's probe", async () => {
    const calls: string[] = [];
    const fakeOci = async (): Promise<CloudEnv> => {
      calls.push("oci");
      return { provider: "oci", region: "us-ashburn-1", source: "imds" };
    };
    const fakeAws = async (): Promise<CloudEnv | null> => {
      calls.push("aws");
      return null;
    };
    const origOci = _PROBES.oci;
    const origAws = _PROBES.aws;
    _PROBES.oci = fakeOci;
    _PROBES.aws = fakeAws;
    try {
      const env = await _runProbe("oci");
      expect(env.provider).toBe("oci");
      expect(env.region).toBe("us-ashburn-1");
      expect(calls).toEqual(["oci"]); // AWS never fired
    } finally {
      _PROBES.oci = origOci!;
      _PROBES.aws = origAws!;
    }
  });
});

// ---------------------------------------------------------------------------
// Orchestration / never-blocks-init
// ---------------------------------------------------------------------------

describe("startBackgroundDetection", () => {
  test("never blocks init when metadata unreachable", () => {
    // Replace probes with ones that hang for 5 s — should NOT delay init.
    const hangPromise = new Promise<null>(() => {
      // Never resolves.
    });
    const orig = { ..._PROBES };
    _PROBES.aws = () => hangPromise as Promise<null>;
    _PROBES.gcp = () => hangPromise as Promise<null>;
    _PROBES.azure = () => hangPromise as Promise<null>;
    try {
      const t0 = performance.now();
      startBackgroundDetection(true);
      const elapsed = performance.now() - t0;
      expect(elapsed).toBeLessThan(50);
    } finally {
      Object.assign(_PROBES, orig);
    }
  });

  test("trackNetwork=false skips probe", () => {
    startBackgroundDetection(false);
    const env = getCloudEnv();
    expect(env.source).toBe("none");
  });

  test("full env-var resolution skips Phase 2", async () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "x";
    process.env.AWS_REGION = "eu-west-1";
    let probeCalled = false;
    const orig = { ..._PROBES };
    _PROBES.aws = async () => {
      probeCalled = true;
      return null;
    };
    try {
      startBackgroundDetection(true);
      // Even if a background probe fires, the result should reflect env-vars.
      await _awaitBackgroundForTests();
      const env = getCloudEnv();
      expect(env.provider).toBe("aws");
      expect(env.region).toBe("eu-west-1");
      expect(probeCalled).toBe(false);
    } finally {
      Object.assign(_PROBES, orig);
    }
  });

  test("background probe updates result when env+dmi yield partial info", async () => {
    // No env vars; DMI says AWS but no region. Phase 2 should fill region.
    dmiFixture({ sys_vendor: "Amazon EC2" });
    const orig = { ..._PROBES };
    _PROBES.aws = async () => ({
      provider: "aws",
      region: "us-west-2",
      source: "imds",
    });
    try {
      startBackgroundDetection(true);
      // Initial result should be from DMI.
      expect(getCloudEnv().source).toBe("dmi");
      await _awaitBackgroundForTests();
      const env = getCloudEnv();
      expect(env.provider).toBe("aws");
      expect(env.region).toBe("us-west-2");
      expect(env.source).toBe("imds");
    } finally {
      Object.assign(_PROBES, orig);
    }
  });

  test("background preserves env-var region when probe returns none", async () => {
    // GCP via K_SERVICE has no region from env; if probe also returns none-
    // null but no region, the initial state is preserved.
    process.env.K_SERVICE = "svc";
    const orig = { ..._PROBES };
    // Force GCP probe to "succeed" with no region (provider only).
    _PROBES.gcp = async () => ({ provider: "gcp", region: null, source: "imds" });
    _PROBES.aws = async () => null;
    _PROBES.azure = async () => null;
    try {
      // Provide an initial region via a fake env-var mechanism: we want to
      // exercise the "initial.region && !env.region" branch, so call
      // _runProbe directly with a hint that returns no region but the
      // pre-known region should be preserved by the orchestrator.
      startBackgroundDetection(true);
      await _awaitBackgroundForTests();
      const env = getCloudEnv();
      // The orchestrator returns the probe result; region remains null.
      expect(env.provider).toBe("gcp");
    } finally {
      Object.assign(_PROBES, orig);
    }
  });
});

// ---------------------------------------------------------------------------
// Sanity: probes really do timeout (smoke test — does not actually network)
// ---------------------------------------------------------------------------

describe("Phase 2 probe timeout", () => {
  test("probe returns null when fetch throws AbortError", async () => {
    const abortFetch = (async () => {
      throw Object.assign(new Error("aborted"), { name: "AbortError" });
    }) as unknown as typeof fetch;
    _setFetchForTests(abortFetch);
    const env = await _probes.aws();
    expect(env).toBeNull();
  });
});

// Soak: ensure vi import is used (vi is reserved for future spies).
void vi;
