/**
 * SDK configuration and API-key infrastructure.
 *
 * Mirrors the Python SDK's `config.py`: API keys must start with
 * `dx_live_` or `dx_test_`, the key is resolved from the
 * `DEXCOST_API_KEY` environment variable when not passed explicitly,
 * and an explicit `storage: "local"` forces local-only mode.
 */

import { createRequire } from "node:module";

/** Raised when an API key has an invalid format. */
export class InvalidAPIKeyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidAPIKeyError";
  }
}

/** Detected key type — `"live"`, `"test"`, or `undefined` when no key. */
export type KeyType = "live" | "test";

/**
 * Validate an API key's format.
 *
 * Returns `"live"`, `"test"`, or `undefined` when `key` is undefined/null.
 * Throws `InvalidAPIKeyError` for any other (non-empty) value.
 */
export function validateApiKey(key: string | undefined | null): KeyType | undefined {
  if (key === undefined || key === null) {
    return undefined;
  }
  if (key.startsWith("dx_live_")) {
    return "live";
  }
  if (key.startsWith("dx_test_")) {
    return "test";
  }
  throw new InvalidAPIKeyError(
    `Invalid API key format: key must start with 'dx_live_' or 'dx_test_', ` +
      `got '${key.slice(0, 10)}...'`,
  );
}

/** Storage mode — `"cloud"` syncs to the Control Layer, `"local"` does not. */
export type StorageMode = "cloud" | "local";

/** Resolved SDK configuration. */
export interface ResolvedConfig {
  /** The effective API key (explicit arg or `DEXCOST_API_KEY` env var). */
  apiKey?: string;
  /** Detected key type. */
  keyType?: KeyType;
  /** True when the key is a test/sandbox key. */
  isSandbox: boolean;
  /** Resolved storage mode. */
  storageMode: StorageMode;
}

/** Validated public-key policy for signed catalog releases. */
export interface ResolvedCatalogTrustPolicy {
  trustedKeys: Readonly<Record<string, string>>;
  requireSignature: boolean;
}

const CATALOG_KEY_ID = /^[a-z0-9][a-z0-9._:-]{0,127}$/u;
const CATALOG_TRUSTED_KEYS_ENV = "DEXCOST_CATALOG_TRUSTED_KEYS";
const CATALOG_REQUIRE_SIGNATURE_ENV = "DEXCOST_CATALOG_REQUIRE_SIGNATURE";

interface BundledCatalogTrust {
  schema_version: string;
  algorithm: string;
  trusted_keys: unknown;
}

function validateCatalogPublicKey(keyId: string, value: unknown): string {
  if (!CATALOG_KEY_ID.test(keyId)) throw new TypeError("catalog trusted key ID is invalid");
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/u.test(value)) {
    throw new TypeError(`catalog trusted key ${keyId} is not unpadded base64url`);
  }
  if (Buffer.from(value, "base64url").byteLength !== 32) {
    throw new TypeError(`catalog trusted key ${keyId} has the wrong byte length`);
  }
  return value;
}

function catalogKeysFromEnvironment(encoded: string | undefined): Record<string, string> {
  if (encoded === undefined) return {};
  if (encoded.trim() === "") throw new TypeError(`${CATALOG_TRUSTED_KEYS_ENV} must not be empty`);
  let value: unknown;
  try {
    value = JSON.parse(encoded) as unknown;
  } catch (error) {
    throw new TypeError(`${CATALOG_TRUSTED_KEYS_ENV} is not valid JSON`, { cause: error });
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${CATALOG_TRUSTED_KEYS_ENV} must contain 1-8 public keys`);
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length < 1 || entries.length > 8) {
    throw new TypeError(`${CATALOG_TRUSTED_KEYS_ENV} must contain 1-8 public keys`);
  }
  return Object.fromEntries(entries.map(([keyId, publicKey]) => [
    keyId,
    validateCatalogPublicKey(keyId, publicKey),
  ]));
}

function bundledCatalogKeys(): Record<string, string> {
  let value: unknown;
  try {
    value = createRequire(import.meta.url)("./catalog-production-trust.json") as unknown;
  } catch (error) {
    throw new TypeError("bundled catalog production trust is unreadable", { cause: error });
  }
  if (
    value === null || typeof value !== "object" || Array.isArray(value) ||
    Object.keys(value).sort().join(",") !== "algorithm,schema_version,trusted_keys"
  ) {
    throw new TypeError("bundled catalog production trust has an invalid schema");
  }
  const trust = value as BundledCatalogTrust;
  if (
    trust.schema_version !== "1" || trust.algorithm !== "ed25519" ||
    trust.trusted_keys === null || typeof trust.trusted_keys !== "object" ||
    Array.isArray(trust.trusted_keys)
  ) {
    throw new TypeError("bundled catalog production trust has an invalid schema");
  }
  const entries = Object.entries(trust.trusted_keys as Record<string, unknown>);
  if (entries.length > 8) {
    throw new TypeError("bundled catalog production trust supports at most 8 public keys");
  }
  return Object.fromEntries(entries.map(([keyId, publicKey]) => [
    keyId,
    validateCatalogPublicKey(keyId, publicKey),
  ]));
}

function catalogSignatureRequirementFromEnvironment(
  encoded: string | undefined,
): boolean | undefined {
  if (encoded === undefined) return undefined;
  const normalized = encoded.trim().toLowerCase();
  if (normalized === "true") return true;
  if (normalized === "false") return false;
  throw new TypeError(`${CATALOG_REQUIRE_SIGNATURE_ENV} must be true or false`);
}

/**
 * Resolve catalog trust without ever silently weakening an invalid policy.
 * Explicit options override their corresponding environment settings. When
 * keys are present and no requirement is specified, signatures are required;
 * accepting unsigned releases needs an explicit migration override.
 */
export function resolveCatalogTrustPolicy(
  trustedKeys?: Readonly<Record<string, string>>,
  requireSignature?: boolean,
): ResolvedCatalogTrustPolicy {
  let resolvedKeys: Record<string, string>;
  if (trustedKeys === undefined) {
    const encodedEnvironmentKeys = process.env[CATALOG_TRUSTED_KEYS_ENV];
    resolvedKeys = encodedEnvironmentKeys === undefined
      ? bundledCatalogKeys()
      : catalogKeysFromEnvironment(encodedEnvironmentKeys);
  } else {
    if (trustedKeys === null || typeof trustedKeys !== "object" || Array.isArray(trustedKeys)) {
      throw new TypeError("catalogTrustedKeys must be an object");
    }
    const entries = Object.entries(trustedKeys);
    if (entries.length > 8) throw new TypeError("catalogTrustedKeys supports at most 8 public keys");
    resolvedKeys = Object.fromEntries(entries.map(([keyId, publicKey]) => [
      keyId,
      validateCatalogPublicKey(keyId, publicKey),
    ]));
  }

  let resolvedRequirement: boolean;
  if (requireSignature === undefined) {
    const environmentRequirement = catalogSignatureRequirementFromEnvironment(
      process.env[CATALOG_REQUIRE_SIGNATURE_ENV],
    );
    resolvedRequirement = environmentRequirement ?? Object.keys(resolvedKeys).length > 0;
  } else if (typeof requireSignature === "boolean") {
    resolvedRequirement = requireSignature;
  } else {
    throw new TypeError("catalogRequireSignature must be a boolean or undefined");
  }
  if (resolvedRequirement && Object.keys(resolvedKeys).length === 0) {
    throw new TypeError("catalog signature verification requires at least one trusted public key");
  }
  return { trustedKeys: Object.freeze({ ...resolvedKeys }), requireSignature: resolvedRequirement };
}

/**
 * Resolve the effective API key and storage mode.
 *
 * @param apiKey - Explicit API key (takes precedence over the env var).
 * @param storage - Explicit storage mode. `"local"` forces local-only and
 *   skips env-var resolution; otherwise the mode is inferred from whether
 *   a key is present.
 */
export function resolveConfig(
  apiKey?: string,
  storage?: StorageMode,
): ResolvedConfig {
  let effectiveKey = apiKey;
  if (effectiveKey === undefined && storage !== "local") {
    effectiveKey = process.env.DEXCOST_API_KEY ?? undefined;
  }

  const keyType = validateApiKey(effectiveKey);

  let storageMode: StorageMode;
  if (storage === "local") {
    storageMode = "local";
  } else if (effectiveKey !== undefined) {
    storageMode = "cloud";
  } else {
    storageMode = "local";
  }

  return {
    apiKey: effectiveKey,
    keyType,
    isSandbox: keyType === "test",
    storageMode,
  };
}
