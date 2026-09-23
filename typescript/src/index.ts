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
export type {
  TrackerOptions, TaskOptions, ToolCallOptions, TrackToolOptions, AmendOutcomeOptions,
} from "./core/tracker.js";

// Singleton / init pattern
export {
  init,
  getTracker,
  globalTrack as track,
  globalStartTask as task,
  globalAttachTask as attachTask,
  globalRecordCost as recordCost,
  globalReportToolCall as reportToolCall,
  globalTrackTool as trackTool,
  globalRecordOutcome as recordOutcome,
  globalGetOutcomeHistory as getOutcomeHistory,
  globalAmendOutcome as amendOutcome,
  globalRecordRevenue as recordRevenue,
  globalGetRevenueHistory as getRevenueHistory,
  globalExplainPricing as explainPricing,
  globalFlush as flush,
  flushBeforeFreeze,
  globalClose as close,
  globalCloseAsync as closeAsync,
  setApiKey,
  globalDeliveryStatus as deliveryStatus,
  globalCatalogStatus as catalogStatus,
  globalImportCatalogBundle as importCatalogBundle,
  globalExportCatalogBundle as exportCatalogBundle,
} from "./core/tracker.js";
export { ToolUsage } from "./core/tool.js";
export type {
  ToolQuantityInput, ToolCostInput, ToolDimensionInput, ToolOperationStatus,
} from "./core/tool.js";
export {
  getCurrentTask,
  setCurrentTask,
  runWithTask,
  setContext,
  getContext,
  clearContext,
  runWithContext,
} from "./core/context.js";
export type { DexcostContext } from "./core/context.js";
export {
  validateCapability,
  capabilityToDict,
  getCapability,
  setCapability,
  runWithCapability,
  runWithCapability as capabilityContext,
  defaultToolCapability,
} from "./core/capabilities.js";
export type {
  CapabilityIdentity, CapabilityKind, CapabilitySource, CapabilityInvocation,
} from "./core/capabilities.js";
export {
  getIdempotencyKey, setIdempotencyKey,
  runWithIdempotencyKey, runWithIdempotencyKey as idempotencyKey,
  idempotencyHash, equivalentIdempotentEvent,
} from "./core/idempotency.js";
export type { IdempotencyKeyToken } from "./core/idempotency.js";
export {
  OutcomeRevision, RevenueRevision, outcomeValue, revenueAmount,
} from "./core/business.js";
export type {
  OutcomeInput, OutcomeState, OutcomeValue, OutcomeValueType,
  RevenueInput, RevenueState, RevenueSource, RevenueSourceType, RevenueAmount,
} from "./core/business.js";
export {
  ProviderJobRevision, providerJobEventId, providerJobFromDict,
} from "./core/provider-jobs.js";
export type {
  ProviderJobRevisionOptions, ProviderJobStatus, ProviderJobUsageLine,
  ProviderJobCostSource, ProviderJobCostConfidence,
} from "./core/provider-jobs.js";
export { toBusinessIdentityRevision } from "./core/business-identity.js";
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
  isoCanonical,
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

// Attribution v2 public contract and v1-capture conversion boundary.
export {
  ATTRIBUTION_V2_CONTRACT_VERSION,
  ATTRIBUTION_COMPONENTS,
  ATTRIBUTION_USAGE_METRICS,
  ATTRIBUTION_USAGE_UNITS,
  ATTRIBUTION_UNIT_BY_METRIC,
} from "./attribution/types.js";
export type {
  AttributionComponent,
  AttributionUsageMetric,
  AttributionUsageUnit,
  AttributionConfidence,
  AttributionLifecycleState,
  AttributionCostEvidenceSource,
  AttributionUsageLineV2,
  AttributionProviderIdentityV2,
  AttributionResourceV2,
  AttributionCostEvidenceV2,
  AttributionLifecycleV2,
  AttributionUsagePeriodV2,
  AttributionEventV2,
  AttributionTaskIngestV1,
} from "./attribution/types.js";
export {
  validateAttributionEventV2,
  assertAttributionEventV2,
} from "./attribution/validate.js";
export type {
  AttributionV2ValidationIssue,
  AttributionV2ValidationResult,
} from "./attribution/validate.js";
export {
  toAttributionEventV2,
  toAttributionTaskIngestV1,
} from "./attribution/convert.js";

// Attribution v3 is the only observation contract emitted by the transport.
export { ATTRIBUTION_V3_CONTRACT_VERSION } from "./attribution/v3-types.js";
export type {
  AttributionBillingDimensionValue,
  AttributionBillingDimension,
  AttributionUsageMetricV3,
  AttributionUsageUnitV3,
  AttributionUsageLineV3,
  AttributionProviderIdentityV3,
  AttributionResourceV3,
  AttributionTraceIdentityV3,
  AttributionAttemptIdentityV3,
  AttributionOperationStatusV3,
  AttributionOperationIdentityV3,
  AttributionCostEvidenceV3,
  AttributionLifecycleV3,
  AttributionUsagePeriodV3,
  AttributionObservationV3,
  AttributionEventV3,
  AttributionCapabilityIdentityV3,
  AttributionCapabilityKindV3,
  AttributionCapabilitySourceV3,
  AttributionCapabilityInvocationV3,
  AttributionOperationErrorV3,
} from "./attribution/v3-types.js";
export {
  validateAttributionObservationV3,
  assertAttributionObservationV3,
} from "./attribution/v3-validate.js";
export type {
  AttributionV3ValidationIssue,
  AttributionV3ValidationResult,
} from "./attribution/v3-validate.js";
export {
  toAttributionObservationV3,
  toAttributionEventV3,
} from "./attribution/v3-convert.js";

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
export {
  DeliveryStatus,
  localDeliveryStatus,
  onDeliveryError,
  removeDeliveryErrorCallback,
} from "./transport/delivery.js";
export type {
  DeliveryWorkerState, DeliveryErrorOperation, DeliveryErrorEvent,
  DeliveryErrorCallback, DeliveryStatusOptions,
} from "./transport/delivery.js";

// Webhook verification
export {
  WebhookVerificationError,
  verifyWebhookSignature,
  assertWebhookSignature,
} from "./webhooks.js";
export type {
  WebhookSecret, WebhookHeader, WebhookVerificationOptions,
} from "./webhooks.js";

// Security
export {
  redactDict,
  hashValue,
  enforceMetadataLimit,
  scrubUrl,
} from "./security/redaction.js";

// Pricing
export { PricingEngine } from "./pricing/engine.js";
export type { CostResult, MeteredCostLine, MeteredCostResult } from "./pricing/engine.js";
export {
  PricingProvenance,
  PricingExplanation,
  registerPricingProvenance,
  pricingProvenanceForEvent,
  applyEventPricingProvenance,
  explainEventPricing,
} from "./pricing/explain.js";
export type {
  PricingExplanationStatus, PricingProvenanceOptions, PricingExplanationOptions,
} from "./pricing/explain.js";

// Rate Registry
export { RateRegistry } from "./pricing/rates.js";
export type { RateEntry, InfrastructureRateEntry } from "./pricing/rates.js";

// Retry Heuristics
export { RetryHeuristicEngine, TRANSIENT_ERRORS, ERROR_LIKELIHOODS } from "./core/heuristics.js";
export type { HeuristicMatch } from "./core/heuristics.js";

// Instruments
export { ALL_SUPPORTED_INSTRUMENTS } from "./instruments/index.js";
export type { InstrumentName } from "./instruments/index.js";
export { globalInstrument as instrument, globalUninstrument as uninstrument } from "./core/tracker.js";
export {
  instrumentOpenAI, uninstrumentOpenAI,
  instrumentAnthropic, uninstrumentAnthropic,
  instrumentVercelAI, uninstrumentVercelAI,
  instrumentGemini, uninstrumentGemini,
  instrumentGoogleGenAI, uninstrumentGoogleGenAI,
  instrumentBedrock, uninstrumentBedrock,
  instrumentCohere, uninstrumentCohere,
  instrumentMcp, uninstrumentMcp,
  instrumentLiteLLM, uninstrumentLiteLLM,
  instrumentOllama, uninstrumentOllama,
  instrumentOpenRouter, uninstrumentOpenRouter,
  instrumentPerplexity, uninstrumentPerplexity,
  instrumentGroq, uninstrumentGroq,
  instrumentFal, uninstrumentFal,
} from "./instruments/public.js";

// Middleware
export { createExpressMiddleware } from "./middleware/express.js";
export type { ExpressMiddlewareOptions } from "./middleware/express.js";
export { dexcostFastifyPlugin } from "./middleware/fastify.js";
export type { FastifyPluginOptions } from "./middleware/fastify.js";
export { createHonoMiddleware } from "./middleware/hono.js";
export type { HonoMiddlewareOptions } from "./middleware/hono.js";
export { DexcostInterceptor } from "./middleware/nestjs.js";
export type { NestInterceptorOptions } from "./middleware/nestjs.js";

// Session
export { SessionManager } from "./core/session.js";

// Service Catalog
export { ServiceCatalog } from "./pricing/service-catalog.js";
export type { ServiceEntry, CostExtractionResult } from "./pricing/service-catalog.js";
export {
  CATALOG_KINDS,
  CATALOG_SDK_CONTRACT_VERSION,
  CATALOG_SIGNATURE_DOMAIN,
  CATALOG_BUNDLE_MAX_BYTES,
  CatalogError,
  CatalogValidationError,
  CatalogDowngradeError,
  CatalogReleaseStore,
  CatalogReleaseClient,
  CatalogOverlayClient,
  parseCatalogManifest,
  parseCatalogOverlay,
  validateCatalogArtifact,
  catalogManifestSigningPayload,
  verifyCatalogManifestSignature,
  encodeCatalogBundle,
  parseCatalogBundle,
} from "./pricing/catalog-releases.js";
export type {
  CatalogKind,
  CatalogChannel,
  CatalogArtifactDescriptor,
  CatalogManifest,
  CatalogSnapshot,
  CatalogRefreshResult,
  WorkspaceRateKind,
  WorkspaceRateOverride,
  CatalogWorkspaceOverlay,
  CatalogOverlayRefreshResult,
  CatalogTrustPolicy,
  ParsedCatalogBundle,
} from "./pricing/catalog-releases.js";
export { CatalogRuntime } from "./pricing/catalog-runtime.js";
export type {
  CatalogRuntimeOptions,
  CatalogRuntimeStatus,
  CatalogRuntimeTargets,
} from "./pricing/catalog-runtime.js";

// Adapters
export {
  createDexcostFetch,
  registerInternalHost,
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

// Queue-worker wrap — one tracked task per consumed job.
export { wrapJobHandler } from "./adapters/worker-wrap.js";
export type { WrapJobHandlerOptions } from "./adapters/worker-wrap.js";

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
export { DexcostSpanProcessor } from "./integrations/otel.js";
export type { DexcostSpanProcessorOptions } from "./integrations/otel.js";
export type {
  DexcostAiMiddlewareOptions,
  DexcostLanguageModelMiddleware,
} from "./integrations/ai-sdk.js";

// Debug mode
export { setDebugMode, isDebugMode } from "./core/debug.js";

// Schema Validation
export { validate, SchemaNotFoundError } from "./schema/validate.js";

// Client Wrappers
export { TrackedOpenAI, TrackedAnthropic, wrapOpenAI, wrapAnthropic } from "./clients.js";
