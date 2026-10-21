import { brotliDecompressSync, gunzipSync, inflateSync } from "node:zlib";

export const VERTEX_NATIVE_BODY_LIMIT = 1024 * 1024;

export interface VertexNativeBodyObserver {
  headers(rawHeaders: unknown): void;
  complete(): void;
  cancel(): void;
}

/** Undici 6 (bundled with Node 20/22) has no body-chunk diagnostic channel.
 * Its diagnostic request exposes onData; guard that shape and observe only this
 * request's existing callback. Never read, clone, drain or replace a Response.
 * If the native shape changes, monetary admission remains unavailable. */
export function observeVertexNativeBody(
  request: object,
  stream: boolean,
  onComplete: (terminal: unknown) => void,
  isActive: () => boolean = () => true,
): VertexNativeBodyObserver | undefined {
  let original: (...args: unknown[]) => unknown;
  try {
    const prototype = Object.getPrototypeOf(request);
    const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, "onData");
    if (Object.hasOwn(request, "onData") || typeof descriptor?.value !== "function" ||
        (request as { onData?: unknown }).onData !== descriptor.value) return undefined;
    original = descriptor.value;
  } catch { return undefined; }

  let captured: Buffer | undefined;
  let size = 0;
  let closed = false;
  let valid = true;
  let headersSeen = false;
  let encoding = "identity";
  const invalidate = (): void => { valid = false; captured = undefined; size = 0; };
  const cancel = (): void => {
    closed = true;
    invalidate();
    try {
      if ((request as { onData?: unknown }).onData === observed) Reflect.deleteProperty(request, "onData");
    } catch { /* Preserve later application instrumentation. */ }
  };
  const observed = function (this: unknown, ...args: unknown[]): unknown {
    try {
      if (!closed && !isActive()) cancel();
      if (!closed && valid) {
        const chunk = args[0];
        if (this !== request || !headersSeen || !(chunk instanceof Uint8Array) ||
            chunk.byteLength > VERTEX_NATIVE_BODY_LIMIT - size) invalidate();
        else if (chunk.byteLength > 0) {
          // The parser may reuse its input buffer after this callback returns.
          // A capped growable buffer also bounds allocation overhead when the
          // network supplies many tiny (or empty) chunks.
          const required = size + chunk.byteLength;
          if (!captured || required > captured.byteLength) {
            const expanded = Buffer.allocUnsafe(Math.min(VERTEX_NATIVE_BODY_LIMIT,
              Math.max(16_384, required, (captured?.byteLength ?? 0) * 2)));
            captured?.copy(expanded, 0, 0, size);
            captured = expanded;
          }
          captured.set(chunk, size);
          size += chunk.byteLength;
        }
      }
    } catch { invalidate(); }
    // Do not swallow a native error or change return/backpressure semantics.
    return original.apply(this, args);
  };
  try {
    Object.defineProperty(request, "onData", { value: observed, writable: true, configurable: true });
    if ((request as { onData?: unknown }).onData !== observed) return undefined;
  } catch { return undefined; }

  return {
    headers(rawHeaders: unknown): void {
      try {
        if (closed) return;
        if (headersSeen || !Array.isArray(rawHeaders) || rawHeaders.length % 2) { invalidate(); return; }
        headersSeen = true;
        let encodingSeen = false;
        const text = (value: unknown): string => {
          if (typeof value === "string") return value;
          if (value instanceof Uint8Array) return Buffer.from(value).toString("latin1");
          throw new Error("Unknown native header representation");
        };
        for (let index = 0; index < rawHeaders.length; index += 2) {
          if (text(rawHeaders[index]).toLowerCase() !== "content-encoding") continue;
          if (encodingSeen) { invalidate(); return; }
          encodingSeen = true;
          encoding = text(rawHeaders[index + 1]).trim().toLowerCase();
          if (!["identity", "gzip", "x-gzip", "deflate", "x-deflate", "br"].includes(encoding)) {
            invalidate(); return;
          }
        }
      } catch { invalidate(); }
    },
    complete(): void {
      if (closed) return;
      try {
        if (!valid || !headersSeen || !captured || size === 0 || !isActive()) return;
        const encoded = captured.subarray(0, size);
        captured = undefined; size = 0;
        const options = { maxOutputLength: VERTEX_NATIVE_BODY_LIMIT };
        const decoded = encoding === "gzip" || encoding === "x-gzip" ? gunzipSync(encoded, options) :
          encoding === "deflate" || encoding === "x-deflate" ? inflateSync(encoded, options) :
            encoding === "br" ? brotliDecompressSync(encoded, options) : encoded;
        if (decoded.byteLength > VERTEX_NATIVE_BODY_LIMIT) return;
        const text = new TextDecoder("utf-8", { fatal: true }).decode(decoded);
        let terminal: unknown;
        if (!stream) terminal = JSON.parse(text);
        else {
          // Match the pinned GenAI parser: only complete data: events are
          // processed; trailing partial data is an error, not a terminal meter.
          const events = text.split(/\r\n\r\n|\n\n|\r\r/);
          if (events.pop()?.trim()) return;
          let dataSeen = false;
          for (const event of events) {
            const trimmed = event.trim();
            if (!trimmed.startsWith("data:")) continue;
            terminal = JSON.parse(trimmed.slice(5).trim());
            dataSeen = true;
          }
          if (!dataSeen) return;
        }
        onComplete(terminal);
      } catch { /* Malformed, oversized or unknown bodies are usage-only. */ }
      finally { cancel(); }
    },
    cancel,
  };
}
