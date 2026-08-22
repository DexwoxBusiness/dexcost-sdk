/** Compile-only public type surface smoke test. */
import {
  CostTracker,
  Decimal,
  RateRegistry,
  SchemaNotFoundError,
  ToolUsage,
  attachTask,
  explainPricing,
  instrumentOpenAI,
  instrumentOpenRouter,
  recordOutcome,
  recordRevenue,
  trackTool,
  type CapabilityIdentity,
  type TaskOptions,
  type ToolCallOptions,
  type InfrastructureRateEntry,
  type AttributionCapabilityKindV3,
  type AttributionCapabilitySourceV3,
  type AttributionCapabilityInvocationV3,
  type AttributionOperationErrorV3,
} from "@dexcost/sdk";
import {
  createExpressMiddleware,
  dexcostFastifyPlugin,
  createHonoMiddleware,
  DexcostInterceptor,
} from "@dexcost/sdk/middleware";
import { dexcostAiMiddleware } from "@dexcost/sdk/integrations/ai-sdk";
import { DexcostCallbackHandler } from "@dexcost/sdk/integrations/langchain";
import { DexcostSpanProcessor } from "@dexcost/sdk/integrations/otel";
import { wrapOpenAI } from "@dexcost/sdk/clients";

const taskOptions: TaskOptions = { taskType: "type-smoke" };
const toolOptions: ToolCallOptions = { operation: "query", usage: ToolUsage.fromInput("1.25") };
const capability: CapabilityIdentity = {
  name: "web-search-v2",
  kind: "tool",
  source: "project",
  sourceId: "search-service",
};
const infrastructureRate: InfrastructureRateEntry = {
  kind: "network", key: "local", per: "gb_transferred", costUsd: new Decimal("0.02"),
};
const capabilityKind: AttributionCapabilityKindV3 = "tool";
const capabilitySource: AttributionCapabilitySourceV3 = "plugin";
const capabilityInvocation: AttributionCapabilityInvocationV3 = "automatic";
const operationError: AttributionOperationErrorV3 = { type: "provider_error", code: "429" };

const tracker = new CostTracker({ autoInstrument: [], trackHttp: false });
tracker.registerRate("maps", "request", "0.005");
tracker.registerInfrastructureRate("network", "local", "gb_transferred", "0.02");
const exactRate: Decimal | undefined = tracker.getInfrastructureRate("network", "LOCAL");
tracker.close();

const publicValues = [
  CostTracker,
  Decimal,
  RateRegistry,
  SchemaNotFoundError,
  ToolUsage,
  attachTask,
  explainPricing,
  instrumentOpenAI,
  instrumentOpenRouter,
  recordOutcome,
  recordRevenue,
  trackTool,
  createExpressMiddleware,
  dexcostFastifyPlugin,
  createHonoMiddleware,
  DexcostInterceptor,
  dexcostAiMiddleware,
  DexcostCallbackHandler,
  DexcostSpanProcessor,
  wrapOpenAI,
];

void taskOptions;
void toolOptions;
void capability;
void infrastructureRate;
void capabilityKind;
void capabilitySource;
void capabilityInvocation;
void operationError;
void exactRate;
void publicValues;
