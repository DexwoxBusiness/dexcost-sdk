import { createServer } from "node:http";
import { connect } from "node:net";
import { Agent } from "undici";

/** Real Node fetch/Undici parsing and diagnostic events, with all sockets
 * confined to this loopback fixture. Never contact a provider or require TLS
 * secrets; the test connector maps logical HTTPS origins to our HTTP server. */
export async function vertexNativeFetchFixture() {
  const nativeFetch = globalThis.fetch;
  let sequence = 0;
  const pending = new Map<string, {
    send(): Promise<Response>;
    metadata?: PropertyDescriptorMap;
  }>();
  const server = createServer(async (request, response) => {
    const item = pending.get(String(request.headers["x-dexcost-fixture-id"]));
    if (!item) { response.writeHead(500).end(); return; }
    try {
      const fixture = await item.send();
      item.metadata = Object.fromEntries(["url", "redirected", "status"].flatMap(key => {
        const descriptor = Object.getOwnPropertyDescriptor(fixture, key);
        return descriptor ? [[key, { ...descriptor, configurable: true }]] : [];
      }));
      response.writeHead(fixture.status, Object.fromEntries(fixture.headers));
      response.flushHeaders();
      const reader = fixture.body?.getReader();
      if (reader) {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            response.write(Buffer.from(value));
          }
        } finally { reader.releaseLock(); }
      }
      response.end();
    } catch { response.destroy(); }
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing loopback fixture address");
  const agent = new Agent({
    connect(_options, callback) {
      const onError = (error: Error) => callback(error, null);
      const socket = connect(address.port, "127.0.0.1", () => {
        socket.off("error", onError);
        callback(null, socket);
      });
      socket.once("error", onError);
      return socket;
    },
  });
  return {
    async fetch(input: string | URL | Request, init: RequestInit | undefined, send: () => Promise<Response>): Promise<Response> {
      const id = String(sequence++);
      const item = { send, metadata: undefined as PropertyDescriptorMap | undefined };
      pending.set(id, item);
      const headers = new Headers(init?.headers);
      headers.set("x-dexcost-fixture-id", id);
      try {
        const result = await nativeFetch(input, {
          ...init, headers, dispatcher: agent,
          // One negative test deliberately changes a native POST into GET.
          // The fixture preserves the method without passing an illegal GET body.
          ...(init?.method === "GET" ? { body: undefined } : {}),
        } as RequestInit);
        if (item.metadata) Object.defineProperties(result, item.metadata);
        return result;
      } finally { pending.delete(id); }
    },
    async close() {
      await agent.destroy();
      await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    },
  };
}
