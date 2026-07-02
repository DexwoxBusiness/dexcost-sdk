/**
 * dexcost TypeScript SDK — Agent Unit Economics for Node.js.
 *
 * Tracks LLM costs, non-LLM service fees, retry waste, and attributes
 * them to customers, projects, and workflows.
 *
 * @example
 * ```typescript
 * import { CostTracker } from 'dexcost';
 *
 * const tracker = new CostTracker();
 * await tracker.track({ taskType: 'summarize', customerId: 'acme' }, async (task) => {
 *   task.recordLlmCall('openai', 'gpt-4o', 800, 150);
 *   task.recordCost('pdf_parser', 0.002);
 * });
 * ```
 */

// Core
export { CostTracker, TrackedTask } from "./core/tracker.js";
export type { TrackerOptions } from "./core/tracker.js";

// Singleton / init pattern
export {
  init,
  getTracker,
  globalTrack as track,
  globalFlush as flush,
  globalClose as close,
  globalCloseAsync as closeAsync,
  setApiKey,
} from "./core/tracker.js";
export {
  getCurrentTask,
  runWithTask,
  setContext,
  getContext,
  clearContext,
  runWithContext,
} from "./core/context.js";
export type { DexcostContext } from "./core/context.js";
export {
  createTask,
  createCostEvent,
  taskToDict,
  eventToDict,
  taskFromDict,
  eventFromDict,
  Decimal,
  toDecimal,
  canonicalDecimal,
  addCost,
} from "./core/models.js";
export type {
  Task,
  CostEvent,
  TaskStatus,
  EventType,
  CostConfidence,
  PricingSource,
  DecimalLike,
} from "./core/models.js";

// Configuration / API-key validation
export {
  validateApiKey,
  resolveConfig,
  InvalidAPIKeyError,
} from "./core/config.js";
export type { KeyType, StorageMode, ResolvedConfig } from "./core/config.js";

// Auto-task
export { createAutoTask, finalizeAutoTask, needsAutoTask } from "./core/auto-task.js";
export { finalizeTaskNetwork } from "./core/network-finalize.js";

// Dev Mode
export { isDevMode } from "./dev-console.js";

// Transport
export { EventBuffer } from "./transport/buffer.js";
export { EventPusher } from "./transport/pusher.js";

// Security
export {
  redactDict,
  hashValue,
  enforceMetadataLimit,
  scrubUrl,
} from "./security/redaction.js";

// Pricing
export { PricingEngine } from "./pricing/engine.js";
export type { CostResult } from "./pricing/engine.js";

// Rate Registry
export { RateRegistry } from "./pricing/rates.js";
export type { RateEntry } from "./pricing/rates.js";

// Retry Heuristics
export { RetryHeuristicEngine, TRANSIENT_ERRORS, ERROR_LIKELIHOODS } from "./core/heuristics.js";
export type { HeuristicMatch } from "./core/heuristics.js";

// Instruments
export { ALL_SUPPORTED_INSTRUMENTS } from "./instruments/index.js";
export type { InstrumentName } from "./instruments/index.js";

// Middleware
export { createExpressMiddleware } from "./middleware/express.js";
export type { ExpressMiddlewareOptions } from "./middleware/express.js";
export { dexcostFastifyPlugin } from "./middleware/fastify.js";
export type { FastifyPluginOptions } from "./middleware/fastify.js";
export { createHonoMiddleware } from "./middleware/hono.js";
export type { HonoMiddlewareOptions } from "./middleware/hono.js";

// Session
export { SessionManager } from "./core/session.js";

// Service Catalog
export { ServiceCatalog } from "./pricing/service-catalog.js";
export type { ServiceEntry, CostExtractionResult } from "./pricing/service-catalog.js";

// Adapters
export {
  registerDomainRate,
  getDomainRates,
  clearDomainRates,
  trackHttp,
  untrackHttp,
  getRecordedEvents,
  clearRecordedEvents,
  getServiceCatalog,
  resetServiceCatalog,
  getSessionManager,
  trackBrowser,
  getBrowserEvents,
  clearBrowserEvents,
  lambdaCost,
  getSupportedRegions,
} from "./adapters/index.js";
export type {
  TrackBrowserOptions,
  LambdaCostResult,
  LambdaCostDetails,
} from "./adapters/index.js";

// Compute handler wraps — serverless capture (Phase 1 compute foundation).
export {
  wrapLambdaHandler,
  wrapCloudRunHandler,
  wrapCloudFunctionsHandler,
  wrapAzureFunctionsHandler,
  wrapVercelHandler,
} from "./adapters/compute-wrap.js";

// GPU handler wraps — serverless GPU capture (Phase 2 GPU foundation).
export {
  wrapModalHandler,
  wrapRunpodHandler,
  wrapReplicateHandler,
} from "./adapters/gpu-wrap.js";

// Integrations
export { DexcostCallbackHandler } from "./integrations/langchain.js";
export { dexcostAiMiddleware } from "./integrations/ai-sdk.js";
export type {
  DexcostAiMiddlewareOptions,
  DexcostLanguageModelMiddleware,
} from "./integrations/ai-sdk.js";

// Debug mode
export { setDebugMode, isDebugMode } from "./core/debug.js";

// Schema Validation
export { validate } from "./schema/validate.js";

// Client Wrappers
export { TrackedOpenAI, TrackedAnthropic } from "./clients.js";
