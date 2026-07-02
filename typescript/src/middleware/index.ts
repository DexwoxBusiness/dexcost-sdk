/**
 * HTTP middleware integrations for automatic request cost tracking.
 *
 * - Express/Connect: {@link createExpressMiddleware}
 * - Fastify:         {@link dexcostFastifyPlugin}
 * - Hono (Node/Bun/Deno): {@link createHonoMiddleware}
 */

export { createExpressMiddleware } from "./express.js";
export type { ExpressMiddlewareOptions } from "./express.js";
export { dexcostFastifyPlugin } from "./fastify.js";
export type { FastifyPluginOptions } from "./fastify.js";
export { createHonoMiddleware } from "./hono.js";
export type { HonoMiddlewareOptions } from "./hono.js";
export { DexcostInterceptor } from "./nestjs.js";
export type { NestInterceptorOptions } from "./nestjs.js";
