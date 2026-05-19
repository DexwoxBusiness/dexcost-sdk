/**
 * SDK configuration and API-key infrastructure.
 *
 * Mirrors the Python SDK's `config.py`: API keys must start with
 * `dx_live_` or `dx_test_`, the key is resolved from the
 * `DEXCOST_API_KEY` environment variable when not passed explicitly,
 * and an explicit `storage: "local"` forces local-only mode.
 */

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
