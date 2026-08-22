import { createHash, createPrivateKey, sign as signBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CATALOG_KINDS,
  CatalogDowngradeError,
  CatalogReleaseClient,
  CatalogReleaseStore,
  catalogManifestSigningPayload,
  encodeCatalogBundle,
  parseCatalogManifest,
  parseCatalogOverlay,
  type CatalogKind,
} from "../src/pricing/catalog-releases.js";
import { CatalogRuntime } from "../src/pricing/catalog-runtime.js";
import { PricingEngine } from "../src/pricing/engine.js";
import type { ComputePricingEngine } from "../src/pricing/compute-pricing.js";
import type { GpuPricingEngine } from "../src/pricing/gpu-pricing.js";
import { getServiceCatalog, resetServiceCatalog } from "../src/adapters/http.js";

const dirs: string[] = [];
const TEST_KEY_ID = "dexcost-test-rfc8032-1";
const TEST_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const TEST_PUBLIC_KEY = Buffer.from(
  "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
  "hex",
).toString("base64url");
const ROTATED_KEY_ID = "dexcost-test-rfc8032-2";
const ROTATED_SEED = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb";
const ROTATED_PUBLIC_KEY = Buffer.from(
  "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
  "hex",
).toString("base64url");
afterEach(() => {
  vi.unstubAllGlobals();
  resetServiceCatalog();
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true });
});

function fixture(name: string): Record<string, unknown> {
  const path = name === "llm_prices"
    ? join(process.cwd(), "src", "pricing", "cost_map.json")
    : name === "observer_rules"
      ? join(process.cwd(), "src", "data", "service_usage_observers.json")
      : join(process.cwd(), "src", "data", `${name}.json`);
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

function release(sequence: number, expires = "2035-01-01T00:00:00Z") {
  const values = Object.fromEntries(CATALOG_KINDS.map((kind) => [
    kind,
    kind === "server_pricing_reference"
      ? { catalog_version: "server-v1", activation_id: "10", source: "test", rule_count: 7 }
      : fixture(kind),
  ])) as Record<CatalogKind, Record<string, unknown>>;
  const artifacts = {} as Record<CatalogKind, Uint8Array>;
  const descriptors: Record<string, unknown> = {};
  for (const kind of CATALOG_KINDS) {
    const raw = new TextEncoder().encode(JSON.stringify(values[kind]));
    artifacts[kind] = raw;
    const count = kind === "observer_rules"
      ? (values[kind].observers as unknown[]).length
      : kind === "server_pricing_reference"
        ? 7
        : Object.keys(values[kind]).filter((key) => key !== "_meta" && key !== "sample_spec").length;
    const sha256 = createHash("sha256").update(raw).digest("hex");
    descriptors[kind] = {
      kind,
      schema_version: "1",
      sha256,
      byte_size: raw.byteLength,
      item_count: count,
      media_type: "application/json",
      path: `/v1/catalogs/artifacts/sha256/${sha256}`,
      sdk_contract: { min: 1, max: 1 },
    };
  }
  const manifest = {
    schema_version: "1",
    release_id: `catalog-release-test-${sequence}`,
    release_sequence: sequence,
    channel: "stable",
    published_at: "2026-01-01T00:00:00Z",
    expires_at: expires,
    safety_policy_version: "2026-07-14.2",
    sdk_contract: { min: 1, max: 1 },
    server_pricing_reference: { catalog_version: "server-v1", activation_id: "10" },
    artifacts: descriptors,
    signatures: [],
  };
  return { manifest, raw: new TextEncoder().encode(JSON.stringify(manifest)), artifacts };
}

function storePath(): string {
  const dir = mkdtempSync(join(tmpdir(), "dexcost-ts-catalog-"));
  dirs.push(dir);
  return join(dir, "catalog.json");
}

function signedRelease(
  sequence: number,
  keys = [{ keyId: TEST_KEY_ID, seed: TEST_SEED }],
  expires = "2035-01-01T00:00:00Z",
) {
  const candidate = release(sequence, expires);
  const parsed = parseCatalogManifest(candidate.raw);
  candidate.manifest.signatures = keys.map(({ keyId, seed }) => {
    const privateKey = createPrivateKey({
      key: Buffer.from(`302e020100300506032b657004220420${seed}`, "hex"),
      format: "der",
      type: "pkcs8",
    });
    const signature = signBytes(null, catalogManifestSigningPayload(parsed), privateKey);
    return {
      algorithm: "ed25519" as const,
      key_id: keyId,
      signature: signature.toString("base64url"),
    };
  });
  candidate.raw = new TextEncoder().encode(JSON.stringify(candidate.manifest));
  return candidate;
}

function jsonResponse(raw: Uint8Array, headers: Record<string, string> = {}): Response {
  return new Response(raw, {
    status: 200,
    headers: {
      "content-type": "application/json",
      "content-length": String(raw.byteLength),
      ...headers,
    },
  });
}

function changeArtifact(candidate: ReturnType<typeof release>, kind: CatalogKind): Uint8Array {
  const value = structuredClone(candidate.manifest);
  const changed = structuredClone(fixture(kind));
  changed._catalog_test_revision = candidate.manifest.release_sequence;
  const raw = new TextEncoder().encode(JSON.stringify(changed));
  const descriptor = value.artifacts[kind] as Record<string, unknown>;
  descriptor.sha256 = createHash("sha256").update(raw).digest("hex");
  descriptor.byte_size = raw.byteLength;
  descriptor.path = `/v1/catalogs/artifacts/sha256/${String(descriptor.sha256)}`;
  candidate.manifest = value;
  candidate.raw = new TextEncoder().encode(JSON.stringify(value));
  candidate.artifacts[kind] = raw;
  return raw;
}

describe("catalog releases", () => {
  it("verifies trusted Ed25519 releases and rejects unsigned or tampered state", () => {
    const store = new CatalogReleaseStore(storePath(), {
      trustedKeys: { [TEST_KEY_ID]: TEST_PUBLIC_KEY },
      requireSignature: true,
    });
    const signed = signedRelease(1);
    expect(store.activate(signed.raw, signed.artifacts).manifest.release_sequence).toBe(1);

    const unsigned = release(2);
    expect(() => store.activate(unsigned.raw, unsigned.artifacts)).toThrow(/requires a trusted signature/);

    signed.manifest.release_sequence = 2;
    signed.manifest.release_id = "catalog-release-test-2";
    signed.raw = new TextEncoder().encode(JSON.stringify(signed.manifest));
    expect(() => store.activate(signed.raw, signed.artifacts)).toThrow(/verification failed/);
  });

  it("accepts a dual-signed rotation release through either overlapping trust key", () => {
    const dualSigned = signedRelease(3, [
      { keyId: TEST_KEY_ID, seed: TEST_SEED },
      { keyId: ROTATED_KEY_ID, seed: ROTATED_SEED },
    ]);
    const oldTrust = new CatalogReleaseStore(storePath(), {
      trustedKeys: { [TEST_KEY_ID]: TEST_PUBLIC_KEY },
      requireSignature: true,
    });
    const newTrust = new CatalogReleaseStore(storePath(), {
      trustedKeys: { [ROTATED_KEY_ID]: ROTATED_PUBLIC_KEY },
      requireSignature: true,
    });

    expect(oldTrust.activate(dualSigned.raw, dualSigned.artifacts).manifest.release_sequence)
      .toBe(3);
    expect(newTrust.activate(dualSigned.raw, dualSigned.artifacts).manifest.release_sequence)
      .toBe(3);
    expect(() => new CatalogReleaseStore(storePath(), {
      trustedKeys: { "dexcost-test-unknown": ROTATED_PUBLIC_KEY },
      requireSignature: true,
    }).activate(dualSigned.raw, dualSigned.artifacts)).toThrow(/not signed by a configured trusted key/);
  });

  it("round-trips a signed air-gap bundle through normal trust checks", () => {
    const policy = {
      trustedKeys: { [TEST_KEY_ID]: TEST_PUBLIC_KEY },
      requireSignature: true,
    };
    const source = new CatalogReleaseStore(storePath(), policy);
    const target = new CatalogReleaseStore(storePath(), policy);
    const signed = signedRelease(4);
    source.activate(signed.raw, signed.artifacts);
    const bundle = source.exportBundle();
    expect(target.importBundle(bundle).manifest.release_sequence).toBe(4);

    const incomplete = JSON.parse(new TextDecoder().decode(bundle)) as {
      artifacts_base64url: Record<string, string>;
    };
    delete incomplete.artifacts_base64url.gpu_prices;
    expect(() => target.importBundle(new TextEncoder().encode(JSON.stringify(incomplete)))).toThrow(/exactly/);
  });

  it("activates all seven artifacts and rejects a downgrade", () => {
    const store = new CatalogReleaseStore(storePath());
    const first = release(1);
    const second = release(2);
    store.activate(first.raw, first.artifacts, '"one"');
    const active = store.activate(second.raw, second.artifacts, '"two"');
    expect(active.manifest.release_sequence).toBe(2);
    expect(Object.keys(active.artifacts).sort()).toEqual([...CATALOG_KINDS].sort());
    expect(() => store.activate(first.raw, first.artifacts)).toThrow(CatalogDowngradeError);
  });

  it("falls back to the previous slot when only the active manifest is corrupt", () => {
    const path = storePath();
    const store = new CatalogReleaseStore(path);
    const first = release(1);
    const second = release(2);
    store.activate(first.raw, first.artifacts);
    store.activate(second.raw, second.artifacts);
    const state = JSON.parse(readFileSync(path, "utf8")) as {
      channels: { stable: { active: { manifest: string } } };
    };
    state.channels.stable.active.manifest = Buffer.from("{").toString("base64");
    writeFileSync(path, JSON.stringify(state), "utf8");
    expect(new CatalogReleaseStore(path).bestAvailable()).toMatchObject({
      source: "previous",
      manifest: { release_sequence: 1 },
    });
  });

  it("falls back to the redundant LKG file when the primary store is corrupt", () => {
    const path = storePath();
    const store = new CatalogReleaseStore(path);
    const first = release(1);
    const second = release(2);
    store.activate(first.raw, first.artifacts);
    store.activate(second.raw, second.artifacts);
    writeFileSync(path, "{power-cut", "utf8");
    expect(new CatalogReleaseStore(path).bestAvailable()?.manifest.release_sequence).toBe(1);
  });

  it("rejects an expired signed bundle without replacing the active LKG", () => {
    const store = new CatalogReleaseStore(storePath(), {
      trustedKeys: { [TEST_KEY_ID]: TEST_PUBLIC_KEY },
      requireSignature: true,
    });
    const current = signedRelease(5);
    store.activate(current.raw, current.artifacts);
    const expired = signedRelease(
      6,
      [{ keyId: TEST_KEY_ID, seed: TEST_SEED }],
      "2026-06-01T00:00:00Z",
    );
    const bundle = encodeCatalogBundle(expired.raw, expired.artifacts);
    expect(() => store.importBundle(bundle)).toThrow(/expired/);
    expect(store.bestAvailable()).toMatchObject({
      source: "active",
      manifest: { release_sequence: 5 },
    });
  });

  it("uses durable state for a 304 without downloading artifacts", async () => {
    const store = new CatalogReleaseStore(storePath());
    const current = release(1);
    store.activate(current.raw, current.artifacts, '"same"');
    const fetchMock = vi.fn(async () => new Response(null, { status: 304 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await new CatalogReleaseClient("https://api.dexcost.io", store).refresh();
    expect(result.status).toBe("not_modified");
    expect(result.snapshot?.manifest.release_sequence).toBe(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("downloads all seven artifacts once and reuses their content hashes", async () => {
    const store = new CatalogReleaseStore(storePath());
    const first = release(1);
    const second = release(2);
    let served = first;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/manifest?")) return jsonResponse(served.raw, { etag: `"${served.manifest.release_sequence}"` });
      const kind = CATALOG_KINDS.find((candidate) =>
        url.endsWith(String((served.manifest.artifacts[candidate] as Record<string, unknown>).sha256)),
      );
      if (kind === undefined) return new Response(null, { status: 404 });
      return jsonResponse(served.artifacts[kind]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new CatalogReleaseClient("https://api.dexcost.io", store);

    expect((await client.refresh()).status).toBe("activated");
    expect(fetchMock).toHaveBeenCalledTimes(1 + CATALOG_KINDS.length);
    served = second;
    expect((await client.refresh()).status).toBe("activated");
    expect(fetchMock).toHaveBeenCalledTimes(2 + CATALOG_KINDS.length);
  });

  it("keeps the active LKG when a downloaded artifact is corrupt", async () => {
    const store = new CatalogReleaseStore(storePath());
    const first = release(1);
    store.activate(first.raw, first.artifacts);
    const second = release(2);
    const changed = changeArtifact(second, "gpu_prices");
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/manifest?")) return jsonResponse(second.raw);
      if (url.endsWith(String((second.manifest.artifacts.gpu_prices as Record<string, unknown>).sha256))) {
        const corrupt = new Uint8Array(changed.byteLength + 1);
        corrupt.set(changed);
        corrupt[corrupt.byteLength - 1] = 0x20;
        return jsonResponse(corrupt);
      }
      return new Response(null, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await new CatalogReleaseClient("https://api.dexcost.io", store).refresh();
    expect(result.status).toBe("failed");
    expect(result.snapshot?.manifest.release_sequence).toBe(1);
    expect(store.active()?.manifest.release_sequence).toBe(1);
  });

  it("rejects channel confusion, compression, malformed length, and expiry", async () => {
    const wrongChannel = release(1);
    wrongChannel.manifest.channel = "canary";
    wrongChannel.raw = new TextEncoder().encode(JSON.stringify(wrongChannel.manifest));
    let response = jsonResponse(wrongChannel.raw);
    vi.stubGlobal("fetch", vi.fn(async () => response));
    let result = await new CatalogReleaseClient("https://api.dexcost.io", new CatalogReleaseStore(storePath())).refresh();
    expect(result.status).toBe("failed");
    expect(result.error).toMatch(/channel does not match/);

    const valid = release(2);
    response = jsonResponse(valid.raw, { "content-encoding": "gzip" });
    result = await new CatalogReleaseClient("https://api.dexcost.io", new CatalogReleaseStore(storePath())).refresh();
    expect(result.error).toMatch(/compressed catalog responses/);

    response = jsonResponse(valid.raw, { "content-length": "not-a-number" });
    result = await new CatalogReleaseClient("https://api.dexcost.io", new CatalogReleaseStore(storePath())).refresh();
    expect(result.error).toMatch(/Content-Length is invalid/);

    const expired = release(3, "2026-06-01T00:00:00Z");
    expect(() => new CatalogReleaseStore(storePath()).activate(expired.raw, expired.artifacts)).toThrow(/expired/);
  });

  it("strictly validates an overlay bound to the active release", () => {
    const current = release(1);
    const store = new CatalogReleaseStore(storePath());
    const snapshot = store.activate(current.raw, current.artifacts);
    const raw = new TextEncoder().encode(JSON.stringify({
      schema_version: "1",
      base_release_id: snapshot.manifest.release_id,
      base_release_sequence: 1,
      generated_at: "2026-02-01T00:00:00Z",
      overrides: [{
        kind: "egress", key: "aws:us-east-1", rate_usd: "0.01",
        per: "gb_egress", notes: null, updated_at: "2026-01-31T00:00:00Z",
      }],
    }));
    expect(parseCatalogOverlay(raw, snapshot.manifest).overrides[0]).toMatchObject({
      kind: "egress", key: "aws:us-east-1", rateUsd: "0.01",
    });
  });

  it("applies one cached release to every in-process consumer without network I/O", () => {
    const path = storePath();
    const current = release(1);
    new CatalogReleaseStore(path).activate(current.raw, current.artifacts);
    const pricing = new PricingEngine();
    let compute: ComputePricingEngine | undefined;
    let gpu: GpuPricingEngine | undefined;
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("provider hot path performed I/O"); }));
    const runtime = new CatalogRuntime({
      pricing,
      replaceCompute: (value) => { compute = value; },
      replaceGpu: (value) => { gpu = value; },
      trackHttp: true,
    }, { endpoint: "https://api.dexcost.io", storePath: path });
    runtime.loadCached();
    expect(pricing.pricingVersion).toMatch(/^catalog-release:1:/);
    expect(compute?.catalogVersion).toMatch(/^catalog-release:1:/);
    expect(gpu?.catalogVersion).toMatch(/^catalog-release:1:/);
    expect(getServiceCatalog()?.catalogVersion).toMatch(/^catalog-release:1:/);
    expect(runtime.status()).toMatchObject({ releaseSequence: 1, source: "active" });
  });
});
