/**
 * PII redaction and metadata safety utilities.
 *
 * Provides field-level redaction, SHA-256 hashing, and metadata size
 * enforcement to protect sensitive data before it leaves the SDK.
 */

import { createHash } from "node:crypto";

/** Default maximum metadata size in bytes (10 KB). */
const DEFAULT_MAX_BYTES = 10_240;

/**
 * Recursively remove specified fields from a dictionary.
 *
 * Returns a new dict with any key found in `fields` stripped entirely at
 * all nesting levels.  Matching is case-sensitive.  This mirrors the
 * Python SDK's `redact_dict`, which DELETES matched keys rather than
 * masking them, so redacted PII never leaves the process.
 */
export function redactDict(
  data: Record<string, unknown>,
  fields: string[]
): Record<string, unknown> {
  const fieldSet = new Set(fields);
  const result: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(data)) {
    if (fieldSet.has(key)) {
      continue;
    }
    if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value)
    ) {
      // Recursively redact nested objects
      result[key] = redactDict(value as Record<string, unknown>, fields);
    } else {
      result[key] = value;
    }
  }

  return result;
}

/**
 * Compute a SHA-256 hex digest of the given value.
 *
 * Uses the Node.js built-in `crypto` module.
 */
export function hashValue(value: string): string {
  return createHash("sha256").update(value, "utf-8").digest("hex");
}

/**
 * Enforce a maximum serialised size on a metadata/details dictionary.
 *
 * If the JSON representation of `details` exceeds `maxBytes`, returns a
 * deterministic stub `{ _truncated: true, _original_size_bytes: N }`
 * rather than partially removing keys.  This mirrors the Python SDK's
 * `enforce_metadata_limit`.  When `details` is unserialisable a
 * `{ _truncated: true, _error: "unserializable" }` stub is returned.
 *
 * @param details - The metadata dictionary to enforce limits on.
 * @param maxBytes - Maximum allowed byte size. Defaults to 10 KB.
 * @returns The original dictionary when within the limit, otherwise a stub.
 */
export function enforceMetadataLimit(
  details: Record<string, unknown>,
  maxBytes: number = DEFAULT_MAX_BYTES
): Record<string, unknown> {
  let serialized: string;
  try {
    serialized = JSON.stringify(details);
  } catch {
    return { _truncated: true, _error: "unserializable" };
  }
  if (serialized === undefined) {
    return { _truncated: true, _error: "unserializable" };
  }
  const byteSize = Buffer.byteLength(serialized, "utf-8");
  if (byteSize <= maxBytes) {
    return details;
  }
  return { _truncated: true, _original_size_bytes: byteSize };
}
