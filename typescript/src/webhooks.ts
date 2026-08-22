import { createHmac, timingSafeEqual } from "node:crypto";

export type WebhookSecret = string | Uint8Array;
export type WebhookHeader = string | readonly string[];

export interface WebhookVerificationOptions {
  timestampHeader?: string;
  secrets: WebhookSecret | readonly WebhookSecret[];
  toleranceSeconds?: number;
  now?: number;
  allowLegacy?: boolean;
}

export class WebhookVerificationError extends Error {
  constructor(message = "invalid or stale DexCost webhook signature") {
    super(message);
    this.name = "WebhookVerificationError";
  }
}

const MAX_SECRETS = 8;
const MAX_SIGNATURES = 16;

function payloadBytes(payload: unknown): Uint8Array {
  if (!(payload instanceof Uint8Array)) {
    throw new TypeError("webhook payload must be the unmodified raw bytes");
  }
  return payload;
}

function secretBytes(value: WebhookVerificationOptions["secrets"]): Uint8Array[] {
  const raw = typeof value === "string" || value instanceof Uint8Array ? [value] : value;
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_SECRETS) {
    throw new TypeError(`webhook verification supports 1 to ${MAX_SECRETS} secrets`);
  }
  return raw.map((secret) => {
    const encoded = typeof secret === "string" ? Buffer.from(secret, "utf8") : secret;
    if (!(encoded instanceof Uint8Array) || encoded.byteLength < 1 || encoded.byteLength > 1024) {
      throw new TypeError("each webhook secret must contain 1 to 1024 bytes");
    }
    return encoded;
  });
}

function headerSignatures(header: WebhookHeader, versioned: boolean): Buffer[] {
  const values = typeof header === "string" ? [header] : header;
  if (!Array.isArray(values)) throw new TypeError("webhook signature header must be a string or sequence");
  const entries = values.flatMap((value) => {
    if (typeof value !== "string") throw new TypeError("webhook signature header values must be strings");
    return value.split(",").map((part) => part.trim()).filter(Boolean);
  });
  if (entries.length > MAX_SIGNATURES) {
    throw new TypeError(`webhook verification supports at most ${MAX_SIGNATURES} signatures`);
  }
  return entries.flatMap((entry) => {
    let digest = entry;
    const separator = entry.indexOf("=");
    if (separator >= 0) {
      const scheme = entry.slice(0, separator).toLowerCase();
      digest = entry.slice(separator + 1);
      if (scheme !== (versioned ? "v1" : "sha256")) return [];
    } else if (versioned) return [];
    if (!/^[0-9a-fA-F]{64}$/.test(digest)) return [];
    return [Buffer.from(digest, "hex")];
  });
}

function timestamp(value: string, toleranceSeconds: number, now?: number): string {
  if (!/^[0-9]+$/.test(value)) throw new TypeError("webhook timestamp must be unix seconds");
  if (!Number.isFinite(toleranceSeconds) || toleranceSeconds < 0 || toleranceSeconds > 86_400) {
    throw new RangeError("webhook toleranceSeconds must be between 0 and 86400");
  }
  const current = now ?? Date.now() / 1000;
  if (!Number.isFinite(current)) throw new TypeError("webhook now must be unix seconds");
  const signedAt = Number(value);
  if (!Number.isSafeInteger(signedAt) || Math.abs(current - signedAt) > toleranceSeconds) {
    throw new RangeError("webhook timestamp is outside the accepted tolerance");
  }
  return value;
}

export function verifyWebhookSignature(
  payload: Uint8Array,
  signatureHeader: WebhookHeader,
  options: WebhookVerificationOptions,
): boolean {
  try {
    const bytes = payloadBytes(payload);
    const secrets = secretBytes(options.secrets);
    const versioned = options.timestampHeader !== undefined;
    if (!versioned && options.allowLegacy !== true) return false;
    const signatures = headerSignatures(signatureHeader, versioned);
    if (signatures.length === 0) return false;
    const message = versioned
      ? Buffer.concat([
        Buffer.from(timestamp(options.timestampHeader!, options.toleranceSeconds ?? 300, options.now), "ascii"),
        Buffer.from("."), Buffer.from(bytes),
      ])
      : Buffer.from(bytes);
    for (const secret of secrets) {
      const expected = createHmac("sha256", secret).update(message).digest();
      for (const received of signatures) {
        if (received.length === expected.length && timingSafeEqual(expected, received)) return true;
      }
    }
    return false;
  } catch {
    return false;
  }
}

export function assertWebhookSignature(
  payload: Uint8Array,
  signatureHeader: WebhookHeader,
  options: WebhookVerificationOptions,
): void {
  if (!verifyWebhookSignature(payload, signatureHeader, options)) {
    throw new WebhookVerificationError();
  }
}
