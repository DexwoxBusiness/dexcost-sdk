import { createHash, createPublicKey, randomUUID, verify as verifyBytes } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { SUPPORTED_SAFETY_POLICY_VERSION, ServiceCatalog } from "./service-catalog.js";
import { ServiceUsageObservers } from "./service-usage-observers.js";

export const CATALOG_SDK_CONTRACT_VERSION = 1;
export const CATALOG_SIGNATURE_DOMAIN = "dexcost.catalog-release.v1\0";
export const CATALOG_KINDS = [
  "observer_rules", "llm_prices", "service_prices", "compute_prices",
  "gpu_prices", "egress_prices", "server_pricing_reference",
] as const;
export type CatalogKind = (typeof CATALOG_KINDS)[number];
export type CatalogChannel = "stable" | "canary";

const MANIFEST_MAX = 256 * 1024;
const ARTIFACT_MAX = 5 * 1024 * 1024;
const RELEASE_MAX = 20 * 1024 * 1024;
const OVERLAY_MAX = 5 * 1024 * 1024;
export const CATALOG_BUNDLE_MAX_BYTES = 32 * 1024 * 1024;
const RELEASE_ID = /^catalog-release-[a-z0-9][a-z0-9._:-]{0,127}$/;
const VERSION = /^[a-z0-9][a-z0-9._:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const DECIMAL_RATE = /^(0|[1-9][0-9]{0,17})(\.[0-9]{1,18})?$/;
const OVERLAY_UNITS: Record<Exclude<WorkspaceRateKind, "service">, ReadonlySet<string>> = {
  compute: new Set([
    "request", "execution", "gb_second", "gib_second", "vcpu_second",
    "active_cpu_hour", "memory_gb_hour", "invocation", "vcpu_hour",
  ]),
  gpu: new Set(["gpu_second", "gpu_hour", "instance_hour", "vgpu_hour"]),
  egress: new Set(["gb_egress", "gb_transferred"]),
};

export class CatalogError extends Error {}
export class CatalogValidationError extends CatalogError {}
export class CatalogDowngradeError extends CatalogValidationError {}

export interface CatalogSdkContract { min: number; max: number }
export interface CatalogArtifactDescriptor {
  kind: CatalogKind;
  schema_version: string;
  sha256: string;
  byte_size: number;
  item_count: number;
  media_type: "application/json";
  path: string;
  sdk_contract: CatalogSdkContract;
}
export interface CatalogManifest {
  schema_version: "1";
  release_id: string;
  release_sequence: number;
  channel: CatalogChannel;
  published_at: string;
  expires_at: string;
  safety_policy_version: string;
  sdk_contract: CatalogSdkContract;
  server_pricing_reference: { catalog_version: string; activation_id: string };
  artifacts: Record<CatalogKind, CatalogArtifactDescriptor>;
  signatures: Array<{ algorithm: "ed25519"; key_id: string; signature: string }>;
}
export interface CatalogSnapshot {
  manifest: CatalogManifest;
  artifacts: Record<CatalogKind, Record<string, unknown>>;
  source: "active" | "previous";
  stale: boolean;
  manifestSha256: string;
}
export interface CatalogRefreshResult {
  status: "activated" | "not_modified" | "failed";
  snapshot?: CatalogSnapshot;
  error?: string;
}
export interface CatalogTrustPolicy {
  /** Raw 32-byte Ed25519 public keys encoded as unpadded base64url. */
  trustedKeys?: Readonly<Record<string, string>>;
  /** Reject unsigned network releases and unsigned durable cache entries. */
  requireSignature?: boolean;
}
export type WorkspaceRateKind = "service" | "compute" | "gpu" | "egress";
export interface WorkspaceRateOverride {
  kind: WorkspaceRateKind;
  key: string;
  rateUsd: string;
  per: string;
  notes?: string;
  updatedAt: string;
}
export interface CatalogWorkspaceOverlay {
  baseReleaseId: string;
  baseReleaseSequence: number;
  generatedAt: string;
  overrides: readonly WorkspaceRateOverride[];
  raw: Uint8Array;
}
export interface CatalogOverlayRefreshResult {
  status: "activated" | "not_modified" | "failed";
  overlay?: CatalogWorkspaceOverlay;
  error?: string;
}

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new CatalogValidationError(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}
function exact(value: unknown, keys: readonly string[], name: string): Record<string, unknown> {
  const result = record(value, name);
  const actual = Object.keys(result).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new CatalogValidationError(`${name} must contain exactly ${expected.join(", ")}`);
  }
  return result;
}
function integer(value: unknown, name: string, min: number, max: number): number {
  if (!Number.isSafeInteger(value) || (value as number) < min || (value as number) > max) {
    throw new CatalogValidationError(`${name} is outside the supported integer range`);
  }
  return value as number;
}
function instant(value: unknown, name: string): string {
  if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) {
    throw new CatalogValidationError(`${name} must be an offset-aware RFC 3339 timestamp`);
  }
  return value;
}
function contract(value: unknown, name: string): CatalogSdkContract {
  const raw = exact(value, ["min", "max"], name);
  const min = integer(raw["min"], `${name}.min`, 1, 10_000);
  const max = integer(raw["max"], `${name}.max`, 1, 10_000);
  if (max < min || CATALOG_SDK_CONTRACT_VERSION < min || CATALOG_SDK_CONTRACT_VERSION > max) {
    throw new CatalogValidationError(`${name} does not support SDK contract 1`);
  }
  return { min, max };
}
function parseJsonBytes(raw: Uint8Array, name: string): unknown {
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)); }
  catch { throw new CatalogValidationError(`${name} is not valid UTF-8 JSON`); }
}

function catalogEndpoint(value: string): URL {
  const url = new URL(value);
  if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) {
    throw new TypeError("catalog endpoint must be an absolute HTTP(S) URL");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new TypeError("catalog endpoint must not contain credentials, query, or fragment");
  }
  return url;
}

async function boundedJsonResponse(response: Response, maxBytes: number): Promise<Uint8Array> {
  const encoding = response.headers.get("content-encoding");
  if (encoding !== null && encoding !== "" && encoding.toLowerCase() !== "identity") {
    throw new CatalogValidationError("compressed catalog responses are not supported");
  }
  const rawLength = response.headers.get("content-length");
  if (rawLength !== null) {
    if (!/^(0|[1-9][0-9]*)$/.test(rawLength)) {
      throw new CatalogValidationError("catalog Content-Length is invalid");
    }
    const length = Number(rawLength);
    if (!Number.isSafeInteger(length) || length > maxBytes) {
      throw new CatalogValidationError("catalog response exceeds size limit");
    }
  }
  if (response.body === null) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > maxBytes) {
        await reader.cancel("catalog response exceeds size limit");
        throw new CatalogValidationError("catalog response exceeds size limit");
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

export function parseCatalogManifest(raw: Uint8Array): CatalogManifest {
  if (raw.byteLength > MANIFEST_MAX) throw new CatalogValidationError("catalog manifest exceeds 256 KiB");
  const top = exact(parseJsonBytes(raw, "catalog manifest"), [
    "schema_version", "release_id", "release_sequence", "channel", "published_at", "expires_at",
    "safety_policy_version", "sdk_contract", "server_pricing_reference", "artifacts", "signatures",
  ], "catalog manifest");
  if (top["schema_version"] !== "1") throw new CatalogValidationError("unsupported manifest schema version");
  if (typeof top["release_id"] !== "string" || !RELEASE_ID.test(top["release_id"])) {
    throw new CatalogValidationError("catalog release_id is invalid");
  }
  const sequence = integer(top["release_sequence"], "release_sequence", 1, Number.MAX_SAFE_INTEGER);
  if (top["channel"] !== "stable" && top["channel"] !== "canary") throw new CatalogValidationError("catalog channel is invalid");
  const published = instant(top["published_at"], "published_at");
  const expires = instant(top["expires_at"], "expires_at");
  if (Date.parse(expires) <= Date.parse(published)) throw new CatalogValidationError("expires_at must follow published_at");
  if (top["safety_policy_version"] !== SUPPORTED_SAFETY_POLICY_VERSION) {
    throw new CatalogValidationError(`unsupported catalog safety policy ${String(top["safety_policy_version"])}`);
  }
  const sdkContract = contract(top["sdk_contract"], "sdk_contract");
  const server = exact(top["server_pricing_reference"], ["catalog_version", "activation_id"], "server_pricing_reference");
  if (typeof server["catalog_version"] !== "string" || server["catalog_version"].length < 1 ||
      typeof server["activation_id"] !== "string" || !/^[1-9]\d*$/.test(server["activation_id"])) {
    throw new CatalogValidationError("server pricing reference is invalid");
  }
  const artifactMap = exact(top["artifacts"], CATALOG_KINDS, "artifacts");
  let releaseBytes = 0;
  const artifacts = {} as Record<CatalogKind, CatalogArtifactDescriptor>;
  for (const kind of CATALOG_KINDS) {
    const value = exact(artifactMap[kind], [
      "kind", "schema_version", "sha256", "byte_size", "item_count", "media_type", "path", "sdk_contract",
    ], `artifacts.${kind}`);
    const sha = value["sha256"];
    if (value["kind"] !== kind || typeof value["schema_version"] !== "string" || !/^[1-9]\d{0,8}$/.test(value["schema_version"]) ||
        typeof sha !== "string" || !SHA256.test(sha) || value["media_type"] !== "application/json" ||
        value["path"] !== `/v1/catalogs/artifacts/sha256/${sha}`) {
      throw new CatalogValidationError(`artifact descriptor ${kind} is invalid`);
    }
    const byteSize = integer(value["byte_size"], `${kind}.byte_size`, 2, ARTIFACT_MAX);
    releaseBytes += byteSize;
    artifacts[kind] = {
      kind, schema_version: value["schema_version"], sha256: sha,
      byte_size: byteSize, item_count: integer(value["item_count"], `${kind}.item_count`, 0, 1_000_000),
      media_type: "application/json", path: value["path"] as string,
      sdk_contract: contract(value["sdk_contract"], `${kind}.sdk_contract`),
    };
  }
  if (releaseBytes > RELEASE_MAX) throw new CatalogValidationError("catalog release exceeds 20 MiB");
  if (!Array.isArray(top["signatures"]) || top["signatures"].length > 8) {
    throw new CatalogValidationError("catalog signatures are invalid");
  }
  const signatures = top["signatures"].map((item, index) => {
    const value = exact(item, ["algorithm", "key_id", "signature"], `signatures[${index}]`);
    if (value["algorithm"] !== "ed25519" || typeof value["key_id"] !== "string" || !VERSION.test(value["key_id"]) ||
        typeof value["signature"] !== "string" || !/^[A-Za-z0-9_-]+$/.test(value["signature"])) {
      throw new CatalogValidationError("catalog signature is invalid");
    }
    return value as unknown as CatalogManifest["signatures"][number];
  });
  return {
    schema_version: "1", release_id: top["release_id"], release_sequence: sequence,
    channel: top["channel"], published_at: published, expires_at: expires,
    safety_policy_version: top["safety_policy_version"] as string, sdk_contract: sdkContract,
    server_pricing_reference: server as unknown as CatalogManifest["server_pricing_reference"],
    artifacts, signatures,
  };
}

function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new CatalogValidationError("catalog signing payload contains a non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const recordValue = value as Record<string, unknown>;
    return `{${Object.keys(recordValue).sort().map((key) => {
      const child = recordValue[key];
      if (child === undefined) throw new CatalogValidationError("catalog signing payload contains undefined");
      return `${JSON.stringify(key)}:${canonicalJson(child)}`;
    }).join(",")}}`;
  }
  throw new CatalogValidationError(`catalog signing payload contains unsupported ${typeof value}`);
}

export function catalogManifestSigningPayload(manifest: CatalogManifest): Uint8Array {
  return new TextEncoder().encode(
    `${CATALOG_SIGNATURE_DOMAIN}${canonicalJson({ ...manifest, signatures: [] })}`,
  );
}

function base64UrlBytes(value: string, size: number, name: string): Buffer {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new CatalogValidationError(`${name} is not unpadded base64url`);
  const decoded = Buffer.from(value, "base64url");
  if (decoded.byteLength !== size) throw new CatalogValidationError(`${name} has the wrong byte length`);
  return decoded;
}

function base64UrlBytesBounded(value: unknown, maximum: number, name: string): Buffer {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new CatalogValidationError(`${name} is not unpadded base64url`);
  }
  const decoded = Buffer.from(value, "base64url");
  if (decoded.byteLength > maximum) throw new CatalogValidationError(`${name} exceeds its byte limit`);
  return decoded;
}

export function verifyCatalogManifestSignature(
  manifest: CatalogManifest,
  policy: CatalogTrustPolicy = {},
): string | undefined {
  if (manifest.signatures.length === 0) {
    if (policy.requireSignature === true) {
      throw new CatalogValidationError("catalog manifest requires a trusted signature");
    }
    return undefined;
  }
  const keys = policy.trustedKeys ?? {};
  if (Object.keys(keys).length === 0) {
    throw new CatalogValidationError("signed catalog manifest has no configured trusted keys");
  }
  const payload = catalogManifestSigningPayload(manifest);
  let matchedKey = false;
  for (const entry of manifest.signatures) {
    const encodedKey = keys[entry.key_id];
    if (encodedKey === undefined) continue;
    matchedKey = true;
    const rawKey = base64UrlBytes(encodedKey, 32, `catalog trusted key ${entry.key_id}`);
    const spki = Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"),
      rawKey,
    ]);
    const key = createPublicKey({ key: spki, format: "der", type: "spki" });
    const signature = base64UrlBytes(entry.signature, 64, `catalog signature ${entry.key_id}`);
    if (verifyBytes(null, payload, key, signature)) return entry.key_id;
  }
  if (!matchedKey) {
    throw new CatalogValidationError("catalog manifest is not signed by a configured trusted key");
  }
  throw new CatalogValidationError("catalog manifest signature verification failed");
}

export interface ParsedCatalogBundle {
  manifest: CatalogManifest;
  manifestRaw: Uint8Array;
  artifacts: Record<CatalogKind, Uint8Array>;
}

export function encodeCatalogBundle(
  manifestRaw: Uint8Array,
  rawArtifacts: Record<CatalogKind, Uint8Array>,
): Uint8Array {
  const manifest = parseCatalogManifest(manifestRaw);
  for (const kind of CATALOG_KINDS) {
    validateCatalogArtifact(manifest, manifest.artifacts[kind], rawArtifacts[kind]);
  }
  const value = {
    schema_version: "1",
    manifest_base64url: Buffer.from(manifestRaw).toString("base64url"),
    artifacts_base64url: Object.fromEntries(CATALOG_KINDS.map((kind) => [
      kind,
      Buffer.from(rawArtifacts[kind]).toString("base64url"),
    ])),
  };
  const raw = new TextEncoder().encode(canonicalJson(value));
  if (raw.byteLength > CATALOG_BUNDLE_MAX_BYTES) {
    throw new CatalogValidationError("catalog bundle exceeds the 32 MiB limit");
  }
  return raw;
}

export function parseCatalogBundle(raw: Uint8Array): ParsedCatalogBundle {
  if (raw.byteLength > CATALOG_BUNDLE_MAX_BYTES) {
    throw new CatalogValidationError("catalog bundle exceeds the 32 MiB limit");
  }
  const top = exact(parseJsonBytes(raw, "catalog bundle"), [
    "schema_version", "manifest_base64url", "artifacts_base64url",
  ], "catalog bundle");
  if (top["schema_version"] !== "1") {
    throw new CatalogValidationError("unsupported catalog bundle schema version");
  }
  const manifestRaw = base64UrlBytesBounded(
    top["manifest_base64url"],
    MANIFEST_MAX,
    "catalog bundle manifest",
  );
  const manifest = parseCatalogManifest(manifestRaw);
  const encodedArtifacts = exact(top["artifacts_base64url"], CATALOG_KINDS, "catalog bundle artifacts");
  const artifacts = {} as Record<CatalogKind, Uint8Array>;
  for (const kind of CATALOG_KINDS) {
    const artifact = base64UrlBytesBounded(
      encodedArtifacts[kind],
      manifest.artifacts[kind].byte_size,
      `catalog bundle artifact ${kind}`,
    );
    validateCatalogArtifact(manifest, manifest.artifacts[kind], artifact);
    artifacts[kind] = artifact;
  }
  return { manifest, manifestRaw, artifacts };
}

function validateMoney(value: unknown, path = "$"): void {
  if (Array.isArray(value)) value.forEach((child, index) => validateMoney(child, `${path}[${index}]`));
  else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (/(usd|cost|rate)/i.test(key) && ["string", "number"].includes(typeof child)) {
        const parsed = Number(child);
        if (!Number.isFinite(parsed) || parsed < 0) throw new CatalogValidationError(`unsafe money value at ${path}.${key}`);
      }
      validateMoney(child, `${path}.${key}`);
    }
  }
}

export function validateCatalogArtifact(
  manifest: CatalogManifest,
  descriptor: CatalogArtifactDescriptor,
  raw: Uint8Array,
): Record<string, unknown> {
  if (raw.byteLength !== descriptor.byte_size) throw new CatalogValidationError(`${descriptor.kind} byte size mismatch`);
  const digest = createHash("sha256").update(raw).digest("hex");
  if (digest !== descriptor.sha256) throw new CatalogValidationError(`${descriptor.kind} SHA-256 mismatch`);
  const value = record(parseJsonBytes(raw, descriptor.kind), descriptor.kind);
  let count: number | undefined;
  if (descriptor.kind === "observer_rules") {
    count = new ServiceUsageObservers(value).observerCount;
  } else if (descriptor.kind === "service_prices") {
    count = new ServiceCatalog(undefined, value).entryCount;
  }
  else if (descriptor.kind === "server_pricing_reference") {
    if (value["catalog_version"] !== manifest.server_pricing_reference.catalog_version ||
        String(value["activation_id"]) !== manifest.server_pricing_reference.activation_id) {
      throw new CatalogValidationError("server pricing artifact does not match manifest");
    }
  } else {
    validateMoney(value);
    const entries = Object.entries(value).filter(([key]) => !["_meta", "sample_spec"].includes(key));
    if (descriptor.kind === "llm_prices" &&
        (entries.length === 0 || entries.some(([, item]) => item === null || typeof item !== "object" || Array.isArray(item)))) {
      throw new CatalogValidationError("llm_prices must contain model objects");
    }
    if (descriptor.kind !== "llm_prices" &&
        (value["_meta"] === null || typeof value["_meta"] !== "object" || Array.isArray(value["_meta"]) ||
         !entries.some(([, item]) => item !== null && typeof item === "object" && !Array.isArray(item)))) {
      throw new CatalogValidationError(`${descriptor.kind} contains no provider pricing`);
    }
    count = entries.length;
  }
  if (count !== undefined && count !== descriptor.item_count) {
    throw new CatalogValidationError(`${descriptor.kind} item_count mismatch`);
  }
  return value;
}

export function parseCatalogOverlay(raw: Uint8Array, manifest: CatalogManifest): CatalogWorkspaceOverlay {
  if (raw.byteLength > OVERLAY_MAX) throw new CatalogValidationError("catalog overlay exceeds 5 MiB");
  const top = exact(parseJsonBytes(raw, "catalog overlay"), [
    "schema_version", "base_release_id", "base_release_sequence", "generated_at", "overrides",
  ], "catalog overlay");
  if (top["schema_version"] !== "1") throw new CatalogValidationError("unsupported catalog overlay schema version");
  if (top["base_release_id"] !== manifest.release_id || top["base_release_sequence"] !== manifest.release_sequence) {
    throw new CatalogValidationError("catalog overlay is not bound to the active release");
  }
  const generatedAt = instant(top["generated_at"], "overlay.generated_at");
  if (!Array.isArray(top["overrides"]) || top["overrides"].length > 100_000) {
    throw new CatalogValidationError("catalog overlay overrides are invalid");
  }
  const identities = new Set<string>();
  const overrides = top["overrides"].map((item, index): WorkspaceRateOverride => {
    const value = exact(item, ["kind", "key", "rate_usd", "per", "notes", "updated_at"], `overlay.overrides[${index}]`);
    const kind = value["kind"];
    const key = value["key"];
    const per = value["per"];
    const rate = value["rate_usd"];
    if (kind !== "service" && kind !== "compute" && kind !== "gpu" && kind !== "egress") {
      throw new CatalogValidationError("catalog overlay kind is invalid");
    }
    if (typeof key !== "string" || key.length < 1 || key.length > 512) {
      throw new CatalogValidationError("catalog overlay key is invalid");
    }
    if (typeof per !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(per) ||
        (kind !== "service" && !OVERLAY_UNITS[kind].has(per))) {
      throw new CatalogValidationError(`unsupported ${kind} overlay billing unit`);
    }
    if (typeof rate !== "string" || !DECIMAL_RATE.test(rate) || !Number.isFinite(Number(rate)) || Number(rate) < 0) {
      throw new CatalogValidationError("catalog overlay rate_usd is invalid");
    }
    const notes = value["notes"];
    if (notes !== null && notes !== undefined && (typeof notes !== "string" || notes.length > 1000)) {
      throw new CatalogValidationError("catalog overlay notes are invalid");
    }
    const updatedAt = instant(value["updated_at"], "overlay override updated_at");
    if (Date.parse(updatedAt) > Date.parse(generatedAt)) {
      throw new CatalogValidationError("catalog overlay generated_at precedes an override");
    }
    const identity = `${kind}\0${key}\0${per}`;
    if (identities.has(identity)) throw new CatalogValidationError("catalog overlay contains a duplicate component");
    identities.add(identity);
    return { kind, key, rateUsd: rate, per, ...(typeof notes === "string" ? { notes } : {}), updatedAt };
  });
  return {
    baseReleaseId: manifest.release_id,
    baseReleaseSequence: manifest.release_sequence,
    generatedAt,
    overrides,
    raw: new Uint8Array(raw),
  };
}

interface StoredRelease {
  manifest: string;
  artifacts: Record<CatalogKind, string>;
  etag?: string;
}
interface StoredOverlay { raw: string; sha256: string; etag?: string }
interface StoreState {
  /** Legacy pre-channel fields are read as stable for forward migration. */
  active?: StoredRelease;
  previous?: StoredRelease;
  channels?: Partial<Record<CatalogChannel, { active?: StoredRelease; previous?: StoredRelease }>>;
  overlays?: Record<string, StoredOverlay>;
}

export class CatalogReleaseStore {
  readonly path: string;
  private readonly trustPolicy: CatalogTrustPolicy;
  constructor(
    path = join(homedir(), ".dexcost", "catalog-releases.json"),
    trustPolicy: CatalogTrustPolicy = {},
  ) {
    this.path = path;
    this.trustPolicy = {
      trustedKeys: { ...(trustPolicy.trustedKeys ?? {}) },
      requireSignature: trustPolicy.requireSignature === true,
    };
    for (const [keyId, value] of Object.entries(this.trustPolicy.trustedKeys ?? {})) {
      if (!VERSION.test(keyId)) throw new CatalogValidationError("catalog trusted key ID is invalid");
      base64UrlBytes(value, 32, `catalog trusted key ${keyId}`);
    }
  }
  private read(): StoreState {
    for (const candidate of [this.path, `${this.path}.lkg`]) {
      try { return JSON.parse(readFileSync(candidate, "utf8")) as StoreState; }
      catch { /* try the redundant state file */ }
    }
    return {};
  }
  private write(state: StoreState): void {
    mkdirSync(dirname(this.path), { recursive: true });
    const temporary = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(temporary, JSON.stringify(state), { encoding: "utf8", mode: 0o600 });
    try { copyFileSync(this.path, `${this.path}.lkg`); } catch { /* first activation */ }
    renameSync(temporary, this.path);
  }
  private channelState(state: StoreState, channel: CatalogChannel): { active?: StoredRelease; previous?: StoredRelease } {
    if (state.channels?.[channel]) return state.channels[channel]!;
    return channel === "stable" ? { active: state.active, previous: state.previous } : {};
  }
  private snapshot(value: StoredRelease | undefined, source: "active" | "previous"): CatalogSnapshot | undefined {
    if (!value) return undefined;
    const manifestRaw = Buffer.from(value.manifest, "base64");
    const manifest = parseCatalogManifest(manifestRaw);
    verifyCatalogManifestSignature(manifest, this.trustPolicy);
    const artifacts = {} as CatalogSnapshot["artifacts"];
    for (const kind of CATALOG_KINDS) {
      const raw = Buffer.from(value.artifacts[kind], "base64");
      artifacts[kind] = validateCatalogArtifact(manifest, manifest.artifacts[kind], raw);
    }
    return {
      manifest, artifacts, source, stale: Date.parse(manifest.expires_at) <= Date.now(),
      manifestSha256: createHash("sha256").update(manifestRaw).digest("hex"),
    };
  }
  active(channel: CatalogChannel = "stable"): CatalogSnapshot | undefined {
    try { return this.snapshot(this.channelState(this.read(), channel).active, "active"); } catch { return undefined; }
  }
  previous(channel: CatalogChannel = "stable"): CatalogSnapshot | undefined {
    try { return this.snapshot(this.channelState(this.read(), channel).previous, "previous"); } catch { return undefined; }
  }
  bestAvailable(channel: CatalogChannel = "stable"): CatalogSnapshot | undefined {
    return this.active(channel) ?? this.previous(channel);
  }
  exportBundle(
    channel: CatalogChannel = "stable",
    source: "active" | "previous" = "active",
  ): Uint8Array {
    const state = this.read();
    const stored = this.channelState(state, channel)[source];
    if (!stored) throw new CatalogValidationError(`no ${source} ${channel} catalog release is available`);
    const manifestRaw = Buffer.from(stored.manifest, "base64");
    const snapshot = this.snapshot(stored, source);
    if (!snapshot) throw new CatalogValidationError("catalog bundle release is unavailable");
    const artifacts = {} as Record<CatalogKind, Uint8Array>;
    for (const kind of CATALOG_KINDS) {
      artifacts[kind] = Buffer.from(stored.artifacts[kind], "base64");
    }
    return encodeCatalogBundle(manifestRaw, artifacts);
  }
  importBundle(raw: Uint8Array, etag?: string): CatalogSnapshot {
    const bundle = parseCatalogBundle(raw);
    return this.activate(bundle.manifestRaw, bundle.artifacts, etag);
  }
  artifactBytes(sha256: string): Uint8Array | undefined {
    if (!SHA256.test(sha256)) throw new CatalogValidationError("catalog artifact SHA-256 is invalid");
    const state = this.read();
    const candidates: Array<StoredRelease | undefined> = [state.active, state.previous];
    for (const channel of Object.values(state.channels ?? {})) {
      candidates.push(channel?.active, channel?.previous);
    }
    for (const release of candidates) {
      if (!release) continue;
      for (const encoded of Object.values(release.artifacts)) {
        const raw = Buffer.from(encoded, "base64");
        if (createHash("sha256").update(raw).digest("hex") === sha256) return raw;
      }
    }
    return undefined;
  }
  etag(channel: CatalogChannel = "stable"): string | undefined {
    return this.channelState(this.read(), channel).active?.etag;
  }
  activate(manifestRaw: Uint8Array, rawArtifacts: Record<CatalogKind, Uint8Array>, etag?: string): CatalogSnapshot {
    const manifest = parseCatalogManifest(manifestRaw);
    verifyCatalogManifestSignature(manifest, this.trustPolicy);
    if (Date.parse(manifest.expires_at) <= Date.now()) throw new CatalogValidationError("catalog release is expired");
    const current = this.active(manifest.channel);
    if (current && manifest.release_sequence < current.manifest.release_sequence) {
      throw new CatalogDowngradeError("catalog release sequence is older than active");
    }
    const manifestSha = createHash("sha256").update(manifestRaw).digest("hex");
    if (current && manifest.release_sequence === current.manifest.release_sequence &&
        manifestSha !== current.manifestSha256) {
      throw new CatalogValidationError("release sequence was reused with different content");
    }
    for (const kind of CATALOG_KINDS) validateCatalogArtifact(manifest, manifest.artifacts[kind], rawArtifacts[kind]);
    const stored: StoredRelease = {
      manifest: Buffer.from(manifestRaw).toString("base64"),
      artifacts: Object.fromEntries(CATALOG_KINDS.map((kind) => [kind, Buffer.from(rawArtifacts[kind]).toString("base64")])) as StoredRelease["artifacts"],
      ...(etag === undefined ? {} : { etag }),
    };
    const old = this.read();
    const oldChannel = this.channelState(old, manifest.channel);
    const nextChannel = {
      active: stored,
      ...(oldChannel.active === undefined ||
          (current && current.manifest.release_sequence === manifest.release_sequence)
        ? (oldChannel.previous === undefined ? {} : { previous: oldChannel.previous })
        : { previous: oldChannel.active }),
    };
    const state: StoreState = {
      channels: { ...(old.channels ?? {}), [manifest.channel]: nextChannel },
      ...(old.overlays === undefined ? {} : { overlays: old.overlays }),
    };
    this.write(state);
    const snapshot = this.active(manifest.channel);
    if (!snapshot) throw new CatalogValidationError("activated catalog could not be revalidated");
    return snapshot;
  }
  private overlayKey(principalSha256: string, releaseId: string): string {
    if (!SHA256.test(principalSha256)) throw new CatalogValidationError("catalog overlay principal hash is invalid");
    return `${principalSha256}:${releaseId}`;
  }
  overlayEtag(principalSha256: string, manifest: CatalogManifest): string | undefined {
    return this.read().overlays?.[this.overlayKey(principalSha256, manifest.release_id)]?.etag;
  }
  loadOverlay(principalSha256: string, manifest: CatalogManifest): CatalogWorkspaceOverlay | undefined {
    const stored = this.read().overlays?.[this.overlayKey(principalSha256, manifest.release_id)];
    if (!stored) return undefined;
    const raw = Buffer.from(stored.raw, "base64");
    if (createHash("sha256").update(raw).digest("hex") !== stored.sha256) {
      throw new CatalogValidationError("durable catalog overlay is corrupt");
    }
    return parseCatalogOverlay(raw, manifest);
  }
  saveOverlay(principalSha256: string, overlay: CatalogWorkspaceOverlay, etag?: string): void {
    const state = this.read();
    const overlays = { ...(state.overlays ?? {}) };
    const key = this.overlayKey(principalSha256, overlay.baseReleaseId);
    overlays[key] = {
      raw: Buffer.from(overlay.raw).toString("base64"),
      sha256: createHash("sha256").update(overlay.raw).digest("hex"),
      ...(etag === undefined ? {} : { etag }),
    };
    this.write({ ...state, overlays });
  }
}

export class CatalogReleaseClient {
  private readonly endpoint: URL;
  constructor(
    endpoint: string,
    readonly store: CatalogReleaseStore,
    readonly channel: CatalogChannel = "stable",
    private readonly timeoutMs = 10_000,
  ) {
    this.endpoint = catalogEndpoint(endpoint);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60_000) {
      throw new TypeError("catalog timeout must be greater than 0 and at most 60000 ms");
    }
  }
  private async get(url: URL, headers: Record<string, string>, maxBytes: number): Promise<{ raw: Uint8Array; etag?: string; notModified: boolean }> {
    if (url.origin !== this.endpoint.origin) throw new CatalogValidationError("catalog artifact changed origin");
    const response = await fetch(url, { headers, redirect: "error", signal: AbortSignal.timeout(this.timeoutMs) });
    if (response.status === 304) return { raw: new Uint8Array(), notModified: true };
    if (!response.ok) throw new CatalogError(`catalog HTTP ${response.status}`);
    const type = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
    if (type !== "application/json") throw new CatalogValidationError("catalog response is not application/json");
    const raw = await boundedJsonResponse(response, maxBytes);
    return { raw, etag: response.headers.get("etag") ?? undefined, notModified: false };
  }
  async refresh(): Promise<CatalogRefreshResult> {
    try {
      const url = new URL("/v1/catalogs/manifest", this.endpoint);
      url.searchParams.set("channel", this.channel);
      url.searchParams.set("sdk_contract", String(CATALOG_SDK_CONTRACT_VERSION));
      const headers: Record<string, string> = {
        Accept: "application/json",
        "Accept-Encoding": "identity",
      };
      const etag = this.store.etag(this.channel);
      if (etag) headers["If-None-Match"] = etag;
      const response = await this.get(url, headers, MANIFEST_MAX);
      if (response.notModified) {
        const snapshot = this.store.bestAvailable(this.channel);
        if (!snapshot) throw new CatalogValidationError("server returned 304 without a durable active release");
        return { status: "not_modified", snapshot };
      }
      const manifest = parseCatalogManifest(response.raw);
      if (manifest.channel !== this.channel) {
        throw new CatalogValidationError("catalog manifest channel does not match request");
      }
      const artifacts = {} as Record<CatalogKind, Uint8Array>;
      for (const kind of CATALOG_KINDS) {
        const descriptor = manifest.artifacts[kind];
        const cached = this.store.artifactBytes(descriptor.sha256);
        if (cached !== undefined) {
          validateCatalogArtifact(manifest, descriptor, cached);
          artifacts[kind] = cached;
          continue;
        }
        const artifactUrl = new URL(descriptor.path, this.endpoint);
        const downloaded = await this.get(artifactUrl, {
          Accept: "application/json",
          "Accept-Encoding": "identity",
        }, descriptor.byte_size);
        if (downloaded.notModified) throw new CatalogValidationError("immutable artifact unexpectedly returned 304");
        validateCatalogArtifact(manifest, descriptor, downloaded.raw);
        artifacts[kind] = downloaded.raw;
      }
      return { status: "activated", snapshot: this.store.activate(response.raw, artifacts, response.etag) };
    } catch (error) {
      return {
        status: "failed", snapshot: this.store.bestAvailable(this.channel),
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }
}

export class CatalogOverlayClient {
  private readonly endpoint: URL;
  private readonly principalSha256: string;
  constructor(endpoint: string, private readonly apiKey: string, private readonly store: CatalogReleaseStore) {
    this.endpoint = catalogEndpoint(endpoint);
    if (!apiKey) throw new TypeError("catalog overlay requires an API key");
    this.principalSha256 = createHash("sha256").update(apiKey).digest("hex");
  }
  cached(manifest: CatalogManifest): CatalogWorkspaceOverlay | undefined {
    return this.store.loadOverlay(this.principalSha256, manifest);
  }
  async refresh(manifest: CatalogManifest): Promise<CatalogOverlayRefreshResult> {
    try {
      const url = new URL("/v1/api/catalogs/overlay", this.endpoint);
      url.searchParams.set("base_release_id", manifest.release_id);
      if (url.origin !== this.endpoint.origin) throw new CatalogValidationError("catalog overlay changed origin");
      const headers: Record<string, string> = {
        Accept: "application/json",
        Authorization: `Bearer ${this.apiKey}`,
      };
      const etag = this.store.overlayEtag(this.principalSha256, manifest);
      if (etag) headers["If-None-Match"] = etag;
      headers["Accept-Encoding"] = "identity";
      const response = await fetch(url, { headers, redirect: "error", signal: AbortSignal.timeout(10_000) });
      if (response.status === 304) {
        const overlay = this.cached(manifest);
        if (!overlay) throw new CatalogValidationError("server returned 304 without a durable overlay");
        return { status: "not_modified", overlay };
      }
      if (!response.ok) throw new CatalogError(`catalog overlay HTTP ${response.status}`);
      const type = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
      if (type !== "application/json") throw new CatalogValidationError("catalog overlay is not application/json");
      const raw = await boundedJsonResponse(response, OVERLAY_MAX);
      const overlay = parseCatalogOverlay(raw, manifest);
      this.store.saveOverlay(this.principalSha256, overlay, response.headers.get("etag") ?? undefined);
      return { status: "activated", overlay };
    } catch (error) {
      let overlay: CatalogWorkspaceOverlay | undefined;
      try { overlay = this.cached(manifest); } catch { overlay = undefined; }
      return {
        status: "failed", overlay,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }
}
