import { createHash, createPrivateKey, sign as signBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import {
  CATALOG_KINDS,
  CatalogReleaseStore,
  catalogManifestSigningPayload,
  encodeCatalogBundle,
  parseCatalogManifest,
} from "../dist/index.js";

const TEST_KEY_ID = "dexcost-test-rfc8032-1";
const TEST_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const TEST_PUBLIC_KEY = Buffer.from(
  "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
  "hex",
).toString("base64url");
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const TYPESCRIPT_ROOT = dirname(SCRIPT_DIR);

function fixture(kind) {
  if (kind === "server_pricing_reference") {
    return { catalog_version: "server-v1", activation_id: "10", source: "probe-selftest", rule_count: 1 };
  }
  const path = kind === "llm_prices"
    ? join(TYPESCRIPT_ROOT, "src", "pricing", "cost_map.json")
    : kind === "observer_rules"
      ? join(TYPESCRIPT_ROOT, "src", "data", "service_usage_observers.json")
      : join(TYPESCRIPT_ROOT, "src", "data", `${kind}.json`);
  return JSON.parse(readFileSync(path, "utf8"));
}

function signedRelease(sequence, { expired = false } = {}) {
  const values = Object.fromEntries(CATALOG_KINDS.map((kind) => [kind, fixture(kind)]));
  const artifacts = {};
  const descriptors = {};
  for (const kind of CATALOG_KINDS) {
    const raw = Buffer.from(JSON.stringify(values[kind]));
    artifacts[kind] = raw;
    const itemCount = kind === "observer_rules"
      ? values[kind].observers.length
      : kind === "server_pricing_reference"
        ? 1
        : Object.keys(values[kind]).filter((key) => key !== "_meta" && key !== "sample_spec").length;
    const sha256 = createHash("sha256").update(raw).digest("hex");
    descriptors[kind] = {
      kind,
      schema_version: "1",
      sha256,
      byte_size: raw.byteLength,
      item_count: itemCount,
      media_type: "application/json",
      path: `/v1/catalogs/artifacts/sha256/${sha256}`,
      sdk_contract: { min: 1, max: 1 },
    };
  }
  const now = Date.now();
  const manifest = {
    schema_version: "1",
    release_id: `catalog-release-probe-${sequence}`,
    release_sequence: sequence,
    channel: "stable",
    published_at: new Date(now - 2 * 86_400_000).toISOString(),
    expires_at: new Date(now + (expired ? -86_400_000 : 30 * 86_400_000)).toISOString(),
    safety_policy_version: "2026-09-03.26",
    sdk_contract: { min: 1, max: 1 },
    server_pricing_reference: { catalog_version: "server-v1", activation_id: "10" },
    artifacts: descriptors,
    signatures: [],
  };
  const unsignedRaw = Buffer.from(JSON.stringify(manifest));
  const parsed = parseCatalogManifest(unsignedRaw);
  const privateKey = createPrivateKey({
    key: Buffer.from(`302e020100300506032b657004220420${TEST_SEED}`, "hex"),
    format: "der",
    type: "pkcs8",
  });
  manifest.signatures = [{
    algorithm: "ed25519",
    key_id: TEST_KEY_ID,
    signature: signBytes(null, catalogManifestSigningPayload(parsed), privateKey).toString("base64url"),
  }];
  const raw = Buffer.from(JSON.stringify(manifest));
  return { raw, artifacts, bundle: encodeCatalogBundle(raw, artifacts), releaseId: manifest.release_id };
}

function runProbe(args) {
  const result = spawnSync(
    process.execPath,
    [join(SCRIPT_DIR, "catalog-release-probe.mjs"), ...args],
    { cwd: TYPESCRIPT_ROOT, encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(`catalog probe failed:\n${result.stdout}${result.stderr}`);
  }
  return JSON.parse(result.stdout.trim());
}

function commonArgs(store, expectedRelease) {
  return [
    "--store", store,
    "--key-id", TEST_KEY_ID,
    "--public-key", TEST_PUBLIC_KEY,
    "--expect-release", expectedRelease,
  ];
}

const directory = mkdtempSync(join(tmpdir(), "dexcost-catalog-probe-"));
try {
  const first = signedRelease(1);
  const second = signedRelease(2);
  const expired = signedRelease(3, { expired: true });
  const importStore = join(directory, "import-store.json");
  const corruptionStore = join(directory, "corruption-store.json");
  const firstBundle = join(directory, "release-1.bundle.json");
  const secondBundle = join(directory, "release-2.bundle.json");
  const expiredBundle = join(directory, "release-3-expired.bundle.json");
  const exportedBundle = join(directory, "exported.bundle.json");
  writeFileSync(firstBundle, first.bundle);
  writeFileSync(secondBundle, second.bundle);
  writeFileSync(expiredBundle, expired.bundle);

  const imported = runProbe([
    ...commonArgs(importStore, second.releaseId),
    "--import-bundle", secondBundle,
    "--export-bundle", exportedBundle,
  ]);
  if (imported.status !== "imported" || imported.source !== "active" || imported.stale) {
    throw new Error("import probe did not report a fresh active release");
  }
  if (!readFileSync(secondBundle).equals(readFileSync(exportedBundle))) {
    throw new Error("probe import/export did not preserve the signed bundle byte-for-byte");
  }

  const tampered = runProbe([
    ...commonArgs(importStore, second.releaseId),
    "--reject-corrupt-bundle", secondBundle,
  ]);
  if (tampered.status !== "corrupt_rejected_lkg_preserved" || tampered.source !== "active") {
    throw new Error("tampered-bundle probe did not preserve active LKG");
  }

  const expiry = runProbe([
    ...commonArgs(importStore, second.releaseId),
    "--reject-expired-bundle", expiredBundle,
  ]);
  if (expiry.status !== "expired_rejected_lkg_preserved" || expiry.source !== "active") {
    throw new Error("expiry probe did not preserve active LKG");
  }

  const seedStore = new CatalogReleaseStore(corruptionStore, {
    trustedKeys: { [TEST_KEY_ID]: TEST_PUBLIC_KEY },
    requireSignature: true,
  });
  seedStore.importBundle(first.bundle);
  seedStore.importBundle(second.bundle);
  const fallback = runProbe([
    ...commonArgs(corruptionStore, first.releaseId),
    "--corrupt-active-store",
  ]);
  if (fallback.status !== "active_corrupt_previous_preserved"
      || fallback.source !== "previous" || fallback.stale) {
    throw new Error("active-corruption probe did not select a fresh previous release");
  }

  process.stdout.write("catalog release probe self-test: 4/4 operations passed\n");
} finally {
  rmSync(directory, { recursive: true, force: true });
}
