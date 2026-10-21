import { describe, expect, it, vi } from "vitest";
import { channel } from "node:diagnostics_channel";
import { createServer } from "node:http";
import { brotliCompressSync, deflateSync, gzipSync } from "node:zlib";
import { observeVertexNativeBody, VERTEX_NATIVE_BODY_LIMIT } from "../src/instruments/vertex-native-body.js";

const nativeFetch = globalThis.fetch;
const terminal = { responseId: "native-response", text: "h\u00e9llo \u4e16\u754c", usageMetadata: { promptTokenCount: 42 } };

class NativeRequest {
  readonly received: unknown[] = [];
  onData(chunk: unknown): boolean { this.received.push(chunk); return false; }
}

async function captureNative(body: Buffer, headers: Record<string, string>, stream = false) {
  const server = createServer((_request, response) => {
    response.writeHead(200, headers);
    response.end(body);
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing local native-body fixture address");
  const origin = `http://127.0.0.1:${address.port}`;
  const completed = vi.fn();
  let request: any;
  let original: any;
  let observer: ReturnType<typeof observeVertexNativeBody>;
  const names = ["undici:request:create", "undici:request:headers", "undici:request:trailers", "undici:request:error"];
  const listeners = names.map(name => {
    const listener = (message: unknown) => {
      const data = message as any;
      if (name === "undici:request:create") {
        if (String(data.request?.origin) !== origin) return;
        request = data.request; original = request.onData;
        observer = observeVertexNativeBody(request, stream, completed);
      } else if (data.request === request) {
        if (name === "undici:request:headers") observer?.headers(data.response.headers);
        else if (name === "undici:request:trailers") observer?.complete();
        else observer?.cancel();
      }
    };
    channel(name).subscribe(listener);
    return listener;
  });
  try {
    const result = await nativeFetch(`${origin}/provider`, { method: "POST", body: "fixture" });
    const output = Buffer.from(await result.arrayBuffer());
    expect(observer).toBeDefined();
    expect(request.onData).toBe(original);
    expect(Object.hasOwn(request, "onData")).toBe(false);
    return { output, completed };
  } finally {
    observer?.cancel();
    names.forEach((name, index) => channel(name).unsubscribe(listeners[index]));
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
}

describe("bounded Vertex native response-body evidence", () => {
  const codecs = [
    ["identity", (body: Buffer) => body],
    ["gzip", gzipSync],
    ["deflate", deflateSync],
    ["br", brotliCompressSync],
  ] as const;
  for (const stream of [false, true]) for (const [encoding, encode] of codecs) {
    it(`observes real native fetch without changing decoded output / stream=${stream} / ${encoding}`, async () => {
      const text = stream
        ? `event: ignored\n\ndata: ${JSON.stringify({ responseId: "interim" })}\r\rdata: ${JSON.stringify(terminal)}\r\n\r\n`
        : JSON.stringify(terminal);
      const bytes = Buffer.from(text);
      const observed = await captureNative(encode(bytes), {
        "content-type": stream ? "text/event-stream" : "application/json",
        "content-encoding": encoding,
      }, stream);
      expect(observed.output.equals(bytes)).toBe(true);
      expect(observed.completed).toHaveBeenCalledExactlyOnceWith(terminal);
    });
  }

  it("accepts absent encoding and preserves original onData receiver, bytes and return value", () => {
    const request = new NativeRequest();
    const original = request.onData;
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    const bytes = Buffer.from(JSON.stringify(terminal));
    expect(request.onData(bytes)).toBe(false);
    expect(request.received).toEqual([bytes]);
    observer.complete();
    expect(completed).toHaveBeenCalledExactlyOnceWith(terminal);
    expect(request.onData).toBe(original);
    expect(Object.hasOwn(request, "onData")).toBe(false);
  });

  it("preserves the exact exception from the original native onData method", () => {
    const error = new Error("native handler failure");
    class ThrowingRequest { onData(_chunk: unknown): never { throw error; } }
    const request = new ThrowingRequest();
    const original = request.onData;
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    let thrown: unknown;
    try { request.onData(Buffer.from(JSON.stringify(terminal))); } catch (cause) { thrown = cause; }
    expect(thrown).toBe(error);
    observer.cancel();
    expect(completed).not.toHaveBeenCalled();
    expect(request.onData).toBe(original);
  });

  for (const encoding of ["x-gzip", "x-deflate"]) it(`supports the native single-encoding alias ${encoding}`, () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([Buffer.from("Content-Encoding"), Buffer.from(encoding)]);
    request.onData((encoding === "x-gzip" ? gzipSync : deflateSync)(Buffer.from(JSON.stringify(terminal))));
    observer.complete();
    expect(completed).toHaveBeenCalledExactlyOnceWith(terminal);
  });

  it.each([
    ["unknown", ["content-encoding", "unknown"]],
    ["stacked", ["content-encoding", "gzip, br"]],
    ["duplicated", ["content-encoding", "gzip", "content-encoding", "gzip"]],
    ["odd header list", ["content-encoding"]],
    ["non-native headers", { "content-encoding": "identity" }],
  ])("leaves %s encoding/header evidence unknown without changing the native call", (_name, headers) => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers(headers);
    const bytes = Buffer.from(JSON.stringify(terminal));
    expect(request.onData(bytes)).toBe(false);
    observer.complete();
    expect(request.received).toEqual([bytes]);
    expect(completed).not.toHaveBeenCalled();
  });

  it("does not admit duplicate response-header callbacks", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]); observer.headers([]);
    request.onData(Buffer.from(JSON.stringify(terminal)));
    observer.complete();
    expect(completed).not.toHaveBeenCalled();
  });

  for (const encoded of [false, true]) it(`bounds ${encoded ? "decoded compressed" : "encoded"} bytes while preserving native fetch`, async () => {
    const bytes = Buffer.from(JSON.stringify({ text: "x".repeat(VERTEX_NATIVE_BODY_LIMIT) }));
    const observed = await captureNative(encoded ? gzipSync(bytes) : bytes, {
      "content-type": "application/json", "content-encoding": encoded ? "gzip" : "identity",
    });
    expect(observed.output.equals(bytes)).toBe(true);
    expect(observed.completed).not.toHaveBeenCalled();
  });

  it.each<[boolean, string]>([
    [false, "{incomplete"],
    [true, `data: ${JSON.stringify(terminal)}`],
    [true, `data: ${JSON.stringify(terminal)}\n\ndata: {incomplete`],
    [true, "event: heartbeat\n\n"],
  ])("requires complete valid JSON/SSE / stream=%s / %s", (stream, text) => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, stream, completed)!;
    observer.headers([]);
    const bytes = Buffer.from(text);
    request.onData(bytes);
    observer.complete();
    expect(request.received).toEqual([bytes]);
    expect(completed).not.toHaveBeenCalled();
  });

  it("parses fragmented UTF-8 and SSE delimiters without consuming or replacing chunks", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, true, completed)!;
    observer.headers([]);
    const bytes = Buffer.from(`data: ${JSON.stringify(terminal)}\r\n\r\n`);
    const chunks = Array.from(bytes, byte => Buffer.from([byte]));
    for (const chunk of chunks) expect(request.onData(chunk)).toBe(false);
    observer.complete();
    expect(request.received).toEqual(chunks);
    expect(completed).toHaveBeenCalledExactlyOnceWith(terminal);
  });

  it("admits an exact-limit JSON body without an off-by-one rejection", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    const overhead = Buffer.byteLength(JSON.stringify({ text: "" }));
    const bytes = Buffer.from(JSON.stringify({ text: "x".repeat(VERTEX_NATIVE_BODY_LIMIT - overhead) }));
    expect(bytes.byteLength).toBe(VERTEX_NATIVE_BODY_LIMIT);
    request.onData(bytes); observer.complete();
    expect(completed).toHaveBeenCalledOnce();
    expect(completed.mock.calls[0][0].text.length).toBe(VERTEX_NATIVE_BODY_LIMIT - overhead);
  });

  it("copies transient native chunks before their input buffers can be reused", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    const bytes = Buffer.from(JSON.stringify(terminal));
    request.onData(bytes); bytes.fill(0); observer.complete();
    expect(completed).toHaveBeenCalledExactlyOnceWith(terminal);
  });

  it.each(["gzip", "deflate", "br"])("invalid %s data never escapes telemetry parsing", encoding => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers(["content-encoding", encoding]);
    const bytes = Buffer.from("not compressed data");
    expect(request.onData(bytes)).toBe(false);
    expect(() => observer.complete()).not.toThrow();
    expect(completed).not.toHaveBeenCalled();
    expect(request.received).toEqual([bytes]);
    expect(Object.hasOwn(request, "onData")).toBe(false);
  });

  it("invalid UTF-8 cannot become a successful JSON meter", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    request.onData(Buffer.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0x22, 0xff, 0x22, 0x7d]));
    observer.complete();
    expect(completed).not.toHaveBeenCalled();
  });

  it("cancellation restores the original native method and makes later completion inert", () => {
    const request = new NativeRequest();
    const original = request.onData;
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    observer.headers([]);
    request.onData(Buffer.from(JSON.stringify(terminal)));
    observer.cancel(); observer.cancel(); observer.complete();
    expect(request.onData).toBe(original);
    expect(Object.hasOwn(request, "onData")).toBe(false);
    expect(completed).not.toHaveBeenCalled();
  });

  it("does not overwrite another owner's onData replacement when cancelled", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    const observer = observeVertexNativeBody(request, false, completed)!;
    const replacement = vi.fn(() => true);
    request.onData = replacement;
    observer.cancel(); observer.complete();
    expect(request.onData).toBe(replacement);
    expect(completed).not.toHaveBeenCalled();
  });

  it("does not admit a body after its generation has become inactive", () => {
    const request = new NativeRequest();
    const completed = vi.fn();
    let active = true;
    const observer = observeVertexNativeBody(request, false, completed, () => active)!;
    observer.headers([]);
    request.onData(Buffer.from(JSON.stringify(terminal)));
    active = false;
    observer.complete();
    expect(completed).not.toHaveBeenCalled();
    expect(Object.hasOwn(request, "onData")).toBe(false);
  });

  it("fails open for non-writable, own-method, and non-extensible request shapes", () => {
    const completed = vi.fn();
    const own = new NativeRequest();
    const ownMethod = vi.fn(() => true);
    Object.defineProperty(own, "onData", { value: ownMethod, writable: false });
    expect(observeVertexNativeBody(own, false, completed)).toBeUndefined();
    expect(own.onData).toBe(ownMethod);
    expect(observeVertexNativeBody(Object.preventExtensions(new NativeRequest()), false, completed)).toBeUndefined();
    expect(observeVertexNativeBody({ onData: ownMethod }, false, completed)).toBeUndefined();
    expect(completed).not.toHaveBeenCalled();
  });
});
