/**
 * PII redaction and metadata safety utilities.
 *
 * Provides field-level redaction, SHA-256 hashing, and metadata size
 * enforcement to protect sensitive data before it leaves the SDK.
 */

import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";

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

/**
 * Canonical set of query parameter names (case-insensitive) that
 * {@link scrubUrl} strips. Must stay in sync with the same set in
 * Python (dexcost/redaction.py), Go (security/redaction.go), and Rust
 * (security/redaction.rs).
 */
const SENSITIVE_QUERY_PARAMS: ReadonlySet<string> = new Set([
  "api_key",
  "apikey",
  "access_token",
  "token",
  "auth",
  "password",
  "secret",
  "signature",
  "x-amz-signature",
  "x-amz-credential",
  "x-amz-security-token",
  "session",
]);

const USERINFO_RE = /^(https?:\/\/)([^@/?#]+@)?(.+)$/;

/**
 * Strip credentials from a URL before it is captured into an event.
 *
 * Removes:
 *  - userinfo (`user:pass@`) from the authority
 *  - query parameters whose name (case-insensitive) is in the canonical
 *    sensitive set OR ends with `-signature`, `-credential`, or
 *    `-security-token` (AWS SigV4 surface)
 *
 * Preserves scheme, host, port, path, non-sensitive query params, and
 * fragment. The shape of every removed query parameter is preserved as
 * `name=REDACTED` so downstream callers can still see which keys were
 * present without leaking the values.
 *
 * Canonical algorithm — Python/Go/Rust SDK implementations must produce
 * byte-identical output for the same input (enforced by
 * /fixtures/expected_outputs/security/).
 */
export function scrubUrl(url: string): string {
  if (!url) return url;
  const m = USERINFO_RE.exec(url);
  if (m) {
    url = m[1] + m[3];
  }

  let fragment = "";
  const hashIdx = url.indexOf("#");
  if (hashIdx >= 0) {
    fragment = url.slice(hashIdx);
    url = url.slice(0, hashIdx);
  }
  const qIdx = url.indexOf("?");
  if (qIdx < 0) return url + fragment;
  const base = url.slice(0, qIdx);
  const query = url.slice(qIdx + 1);

  const parts = query.split("&").map((part) => {
    const eqIdx = part.indexOf("=");
    const name = eqIdx >= 0 ? part.slice(0, eqIdx) : part;
    const lname = name.toLowerCase();
    const sensitive =
      SENSITIVE_QUERY_PARAMS.has(lname) ||
      lname.endsWith("-signature") ||
      lname.endsWith("-credential") ||
      lname.endsWith("-security-token");
    return sensitive ? `${name}=REDACTED` : part;
  });
  return `${base}?${parts.join("&")}${fragment}`;
}
