/**
 * Cloud-environment detection for egress pricing.
 *
 * Mirrors `python/src/dexcost/cloud_detect.py` exactly — env-var names,
 * DMI strings, and IMDS endpoints have all been verified against May-2026
 * docs and MUST NOT be changed in this port.
 *
 * Phase 1a — env-var detection  (sub-millisecond, synchronous).
 * Phase 1b — DMI vendor check   (~1 ms, Linux-only — silent no-op elsewhere).
 * Phase 2  — background metadata probe (fire-and-forget Promise, ~250 ms
 *            budget, never blocks `dexcost.init()`).
 *
 * Notes — research May 2026:
 * - AWS Lambda / Fargate / App Runner set `AWS_REGION` automatically; ECS
 *   (Fargate and on-EC2) also sets `ECS_CONTAINER_METADATA_URI_V4`.
 * - Azure Container Apps embeds the region in `CONTAINER_APP_HOSTNAME` and
 *   `CONTAINER_APP_ENV_DNS_SUFFIX` as `<host>.<REGION>.azurecontainerapps.io`.
 * - GCP Cloud Run / Cloud Functions Gen2 / App Engine do NOT expose a region
 *   env var; region must come from the metadata server (Phase 2).
 * - AWS IMDSv2 has a default HTTP hop-limit of 1, which prevents Docker/Pod
 *   containers from reaching the metadata service; the probe fails silent.
 */

import { readFileSync } from "node:fs";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface CloudEnv {
  provider: string | null;
  region: string | null;
  source: "env" | "dmi" | "imds" | "none";
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PROBE_TIMEOUT_MS = 250;

/** DMI fields read from /sys/class/dmi/id/<field> (Linux only). */
const DMI_FIELDS = [
  "sys_vendor",
  "board_vendor",
  "product_name",
  "chassis_asset_tag",
  "bios_vendor",
  "product_serial",
] as const;

type DmiField = (typeof DMI_FIELDS)[number];
type MatchMode = "eq" | "contains";

interface DmiRule {
  field: DmiField;
  needle: string;
  mode: MatchMode;
  provider: string;
}

/**
 * DMI rules — ordered from most-specific to most-generic; the first match
 * wins. Transcribed from cloud-init's ds-identify (canonical) plus provider
 * documentation, verified May 2026.
 *
 * Canonical signals (chassis_asset_tag / product_name) appear first so
 * they win over loose sys_vendor backups when both are present.
 */
const DMI_RULES: ReadonlyArray<DmiRule> = [
  // Canonical signals first
  { field: "chassis_asset_tag", needle: "oraclecloud.com", mode: "eq", provider: "oci" },
  { field: "chassis_asset_tag", needle: "7783-7084-3265-9085-8269-3286-77", mode: "eq", provider: "azure" },
  { field: "product_name", needle: "google compute engine", mode: "eq", provider: "gcp" },
  { field: "product_name", needle: "alibaba cloud ecs", mode: "eq", provider: "alibaba" },

  // sys_vendor exact matches
  { field: "sys_vendor", needle: "amazon ec2", mode: "eq", provider: "aws" },
  { field: "sys_vendor", needle: "digitalocean", mode: "eq", provider: "digitalocean" },
  { field: "sys_vendor", needle: "hetzner", mode: "eq", provider: "hetzner" },
  { field: "sys_vendor", needle: "vultr", mode: "eq", provider: "vultr" },
  { field: "sys_vendor", needle: "scaleway", mode: "eq", provider: "scaleway" },
  { field: "sys_vendor", needle: "microsoft corporation", mode: "eq", provider: "azure" },

  // Looser substring backups
  { field: "sys_vendor", needle: "amazon", mode: "contains", provider: "aws" },
  { field: "sys_vendor", needle: "google", mode: "contains", provider: "gcp" },
  { field: "sys_vendor", needle: "alibaba cloud", mode: "contains", provider: "alibaba" },
  { field: "sys_vendor", needle: "ovh", mode: "contains", provider: "ovh" },
];

// ---------------------------------------------------------------------------
// Module-level result (single-threaded JS — no lock needed)
// ---------------------------------------------------------------------------

let _result: CloudEnv = { provider: null, region: null, source: "none" };
let _backgroundPromise: Promise<void> | null = null;

export function getCloudEnv(): CloudEnv {
  return _result;
}

function _setResult(env: CloudEnv): void {
  _result = env;
}

// Test-only — reset module state.
export function _resetCloudDetectForTests(): void {
  _result = { provider: null, region: null, source: "none" };
  _backgroundPromise = null;
}

// ---------------------------------------------------------------------------
// Phase 1a — environment variable detection
// ---------------------------------------------------------------------------

const _AZ_CA_REGION_RE = /\.([a-z0-9-]+)\.azurecontainerapps\.io$/i;

function _azureContainerAppsRegion(): string | null {
  for (const name of ["CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX"]) {
    const value = process.env[name];
    if (!value) continue;
    const match = _AZ_CA_REGION_RE.exec(value);
    if (match && match[1]) return match[1].toLowerCase();
  }
  return null;
}

function _detectEnv(): CloudEnv | null {
  const env = process.env;

  // ── GPU / ML clouds (zero egress) ───────────────────────────────────────
  // Detection prevents the universal $0.09/GB default from over-attributing
  // on platforms whose marketing point is $0 egress.

  // Modal (modal.com/docs).
  if (env.MODAL_TASK_ID || env.MODAL_IMAGE_ID) {
    return { provider: "modal", region: env.MODAL_REGION || null, source: "env" };
  }

  // RunPod (docs.runpod.io).
  if (env.RUNPOD_POD_ID || env.RUNPOD_POD_HOSTNAME) {
    return { provider: "runpod", region: env.RUNPOD_DC_ID || null, source: "env" };
  }

  // ── PaaS app platforms ──────────────────────────────────────────────────
  // Render — RENDER=true, RENDER_SERVICE_ID. No region env var.
  if (env.RENDER || env.RENDER_SERVICE_ID) {
    return { provider: "render", region: null, source: "env" };
  }

  // Railway — RAILWAY_PROJECT_ID + RAILWAY_REPLICA_REGION (NOT RAILWAY_REGION).
  if (env.RAILWAY_PROJECT_ID || env.RAILWAY_ENVIRONMENT_ID) {
    return {
      provider: "railway",
      region: env.RAILWAY_REPLICA_REGION || null,
      source: "env",
    };
  }

  // Heroku — DYNO ("web.1" / "worker.1" / "scheduler.x").
  if (env.DYNO) {
    return { provider: "heroku", region: null, source: "env" };
  }

  // Koyeb — KOYEB_APP_NAME / KOYEB_SERVICE_NAME plus KOYEB_REGION (runtime).
  if (env.KOYEB_SERVICE_NAME || env.KOYEB_APP_NAME) {
    return { provider: "koyeb", region: env.KOYEB_REGION || null, source: "env" };
  }

  // ── Fly.io ──────────────────────────────────────────────────────────────
  if (env.FLY_REGION || env.FLY_APP_NAME) {
    return { provider: "fly", region: env.FLY_REGION || null, source: "env" };
  }

  // ── Vercel ──────────────────────────────────────────────────────────────
  // Vercel also exports AWS_REGION; detecting Vercel first surfaces the
  // platform's $0-egress attribution rather than the underlying AWS rate.
  if (env.VERCEL || env.VERCEL_REGION) {
    return { provider: "vercel", region: env.VERCEL_REGION || null, source: "env" };
  }

  // ── AWS ─────────────────────────────────────────────────────────────────
  if (
    env.AWS_LAMBDA_FUNCTION_NAME ||
    env.AWS_EXECUTION_ENV ||
    env.ECS_CONTAINER_METADATA_URI_V4 ||
    env.ECS_CONTAINER_METADATA_URI ||
    env.AWS_REGION ||
    env.AWS_DEFAULT_REGION
  ) {
    const region = env.AWS_REGION || env.AWS_DEFAULT_REGION || null;
    return { provider: "aws", region, source: "env" };
  }

  // ── Azure ───────────────────────────────────────────────────────────────
  if (env.WEBSITE_SITE_NAME || env.FUNCTIONS_WORKER_RUNTIME || env.CONTAINER_APP_NAME) {
    const region = env.REGION_NAME || _azureContainerAppsRegion() || null;
    return { provider: "azure", region, source: "env" };
  }

  // ── GCP ─────────────────────────────────────────────────────────────────
  // K_SERVICE / K_CONFIGURATION are reserved by Cloud Run + Cloud Functions
  // Gen2; FUNCTION_TARGET / FUNCTION_NAME by Cloud Functions Gen1.
  if (
    env.K_SERVICE ||
    env.K_CONFIGURATION ||
    env.GAE_ENV ||
    env.FUNCTION_TARGET ||
    env.FUNCTION_NAME
  ) {
    return { provider: "gcp", region: null, source: "env" };
  }

  return null;
}

// ---------------------------------------------------------------------------
// Phase 1b — DMI check (Linux only; silent no-op elsewhere)
// ---------------------------------------------------------------------------

/**
 * Read all DMI fields we care about. Missing files / non-Linux platforms
 * silently yield an empty record.
 */
function _readDmi(): Record<string, string> {
  if (process.platform !== "linux") return {};
  const result: Record<string, string> = {};
  for (const field of DMI_FIELDS) {
    try {
      const raw = readFileSync(`/sys/class/dmi/id/${field}`, "utf-8");
      result[field] = raw.trim().toLowerCase();
    } catch {
      // Missing file or read error → skip this field.
    }
  }
  return result;
}

// Test seam — allow tests to override `_readDmi` behaviour.
let _readDmiOverride: (() => Record<string, string>) | null = null;
export function _setReadDmiForTests(fn: (() => Record<string, string>) | null): void {
  _readDmiOverride = fn;
}

function _readDmiSafe(): Record<string, string> {
  if (_readDmiOverride !== null) return _readDmiOverride();
  return _readDmi();
}

function _detectDmi(): CloudEnv | null {
  const dmi = _readDmiSafe();
  for (const rule of DMI_RULES) {
    const value = dmi[rule.field];
    if (!value) continue;
    if (rule.mode === "eq" && value === rule.needle) {
      return { provider: rule.provider, region: null, source: "dmi" };
    }
    if (rule.mode === "contains" && value.includes(rule.needle)) {
      return { provider: rule.provider, region: null, source: "dmi" };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Phase 2 — metadata probes
// ---------------------------------------------------------------------------

/**
 * Strip a GCP metadata-server `projects/.../<X>/<name>` response to a region.
 *
 * Zone form  (`projects/123/zones/us-central1-a`) → `us-central1` when
 *   `dropZoneLetter=true`.
 * Region form (`projects/123/regions/us-central1`) → `us-central1` when
 *   `dropZoneLetter=false`.
 */
export function _gcpPathToRegion(value: string, dropZoneLetter: boolean): string | null {
  if (!value) return null;
  const parts = value.split("/");
  const last = parts[parts.length - 1] || "";
  if (!last) return null;
  if (dropZoneLetter) {
    if (!last.includes("-")) return null;
    return last.substring(0, last.lastIndexOf("-"));
  }
  return last;
}

/** Hookable fetch — tests replace this to avoid real network calls. */
let _fetchImpl: typeof fetch = (...args) => fetch(...args);
export function _setFetchForTests(impl: typeof fetch | null): void {
  _fetchImpl = impl ?? ((...args) => fetch(...args));
}

async function _probeAws(): Promise<CloudEnv | null> {
  try {
    const tokenResp = await _fetchImpl("http://169.254.169.254/latest/api/token", {
      method: "PUT",
      headers: { "X-aws-ec2-metadata-token-ttl-seconds": "21600" },
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!tokenResp.ok) return null;
    const token = (await tokenResp.text()).trim();
    if (!token) return null;
    const regResp = await _fetchImpl(
      "http://169.254.169.254/latest/meta-data/placement/region",
      {
        headers: { "X-aws-ec2-metadata-token": token },
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      },
    );
    if (!regResp.ok) return null;
    const region = (await regResp.text()).trim();
    return { provider: "aws", region: region || null, source: "imds" };
  } catch {
    return null;
  }
}

async function _probeGcp(): Promise<CloudEnv | null> {
  const headers = { "Metadata-Flavor": "Google" };
  // Try /region first (works on Cloud Run / Cloud Functions Gen2 and GCE).
  try {
    const resp = await _fetchImpl(
      "http://metadata.google.internal/computeMetadata/v1/instance/region",
      { headers, signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) },
    );
    if (resp.ok) {
      const body = (await resp.text()).trim();
      const region = _gcpPathToRegion(body, false);
      if (region) return { provider: "gcp", region, source: "imds" };
    }
  } catch {
    // Fall through to /zone.
  }

  try {
    const resp = await _fetchImpl(
      "http://metadata.google.internal/computeMetadata/v1/instance/zone",
      { headers, signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) },
    );
    if (!resp.ok) return null;
    const body = (await resp.text()).trim();
    return {
      provider: "gcp",
      region: _gcpPathToRegion(body, true),
      source: "imds",
    };
  } catch {
    return null;
  }
}

async function _probeAzure(): Promise<CloudEnv | null> {
  try {
    const resp = await _fetchImpl(
      "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
      {
        headers: { Metadata: "true" },
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      },
    );
    if (!resp.ok) return null;
    const payload = (await resp.json()) as { compute?: { location?: string } };
    const region = payload.compute?.location || null;
    return { provider: "azure", region, source: "imds" };
  } catch {
    return null;
  }
}

async function _probeOci(): Promise<CloudEnv | null> {
  try {
    const resp = await _fetchImpl(
      "http://169.254.169.254/opc/v2/instance/canonicalRegionName",
      {
        headers: { Authorization: "Bearer Oracle" },
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      },
    );
    if (!resp.ok) return null;
    const region = (await resp.text()).trim().toLowerCase();
    return { provider: "oci", region: region || null, source: "imds" };
  } catch {
    return null;
  }
}

async function _probeDigitalOcean(): Promise<CloudEnv | null> {
  try {
    const resp = await _fetchImpl("http://169.254.169.254/metadata/v1/region", {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!resp.ok) return null;
    const region = (await resp.text()).trim().toLowerCase();
    return { provider: "digitalocean", region: region || null, source: "imds" };
  } catch {
    return null;
  }
}

async function _probeAlibaba(): Promise<CloudEnv | null> {
  try {
    const resp = await _fetchImpl("http://100.100.100.200/latest/meta-data/region-id", {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!resp.ok) return null;
    const region = (await resp.text()).trim().toLowerCase();
    return { provider: "alibaba", region: region || null, source: "imds" };
  } catch {
    return null;
  }
}

/**
 * Provider hint → metadata probe. Mutable so tests can stub individual
 * probes via `monkeypatch.setitem` (Python idiom) → `_PROBES["aws"] = …`.
 */
export const _PROBES: Record<string, () => Promise<CloudEnv | null>> = {
  aws: _probeAws,
  gcp: _probeGcp,
  azure: _probeAzure,
  oci: _probeOci,
  digitalocean: _probeDigitalOcean,
  alibaba: _probeAlibaba,
};

/**
 * Phase 2 fanout providers. When DMI doesn't pre-classify, we race AWS/GCP/
 * Azure in parallel — adding OCI/DO/Alibaba would lengthen worst-case wait
 * and hit the wrong metadata server (DO uses the same 169.254.169.254 IP).
 */
export const _FANOUT_PROBES = ["aws", "gcp", "azure"] as const;

async function _runProbe(providerHint: string | null): Promise<CloudEnv> {
  if (providerHint && providerHint in _PROBES) {
    const probe = _PROBES[providerHint];
    if (probe) {
      const env = await probe();
      return env ?? { provider: providerHint, region: null, source: "imds" };
    }
  }

  // Race the fanout — first non-null wins.
  const promises = _FANOUT_PROBES.map((name) =>
    _PROBES[name]!().then((env) => (env ? env : null)),
  );

  // Promise.race resolves on first settle; we want first NON-null. Use a
  // promise pattern where each non-null result resolves the outer promise.
  const result = await new Promise<CloudEnv | null>((resolve) => {
    let pending = promises.length;
    let settled = false;
    for (const p of promises) {
      p.then(
        (env) => {
          if (settled) return;
          if (env !== null) {
            settled = true;
            resolve(env);
            return;
          }
          pending -= 1;
          if (pending === 0 && !settled) {
            settled = true;
            resolve(null);
          }
        },
        () => {
          if (settled) return;
          pending -= 1;
          if (pending === 0 && !settled) {
            settled = true;
            resolve(null);
          }
        },
      );
    }
  });

  if (result) return result;
  return { provider: null, region: null, source: "none" };
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

/** Run Phase 1a + 1b synchronously. Used by tests; never calls IMDS. */
export function detectNow(): CloudEnv {
  const envResult = _detectEnv();
  if (envResult && envResult.provider !== null && envResult.region !== null) {
    return envResult;
  }
  const dmi = _detectDmi();
  const merged = envResult ?? dmi;
  return merged ?? { provider: null, region: null, source: "none" };
}

/**
 * Resolve provider/region without blocking. Idempotent.
 *
 * When `trackNetwork` is false, no probe is launched.
 *
 * Phase 1a + 1b run synchronously; Phase 2 is fire-and-forget so init()
 * returns immediately. Tests can await `_backgroundPromise` via the
 * `_awaitBackgroundForTests` helper.
 */
export function startBackgroundDetection(trackNetwork: boolean = true): void {
  if (!trackNetwork) {
    _setResult({ provider: null, region: null, source: "none" });
    return;
  }

  const initial = detectNow();
  _setResult(initial);

  if (initial.provider !== null && initial.region !== null) {
    return;
  }

  // Don't launch a second background probe if one is already in flight.
  if (_backgroundPromise !== null) return;

  _backgroundPromise = (async () => {
    try {
      const env = await _runProbe(initial.provider);
      if (env.provider !== null) {
        let final = env;
        // Preserve a region we already had from env-vars if the probe didn't
        // return one (matches Python behaviour).
        if (initial.region && !env.region) {
          final = { provider: env.provider, region: initial.region, source: env.source };
        }
        _setResult(final);
      }
    } catch {
      // Fail silent — Phase 2 is best-effort.
    } finally {
      _backgroundPromise = null;
    }
  })();
}

/** Test-only helper to await the background Phase 2 probe. */
export async function _awaitBackgroundForTests(): Promise<void> {
  if (_backgroundPromise) {
    await _backgroundPromise;
  }
}

// Exposed for tests that need to call probes directly (parity with the
// Python `cd._probe_gcp()` test pattern).
export const _probes = {
  aws: _probeAws,
  gcp: _probeGcp,
  azure: _probeAzure,
  oci: _probeOci,
  digitalocean: _probeDigitalOcean,
  alibaba: _probeAlibaba,
};

export { _runProbe };
