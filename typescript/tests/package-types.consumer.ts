/** Compile-only public type surface smoke test. */
import {
  CostTracker,
  Decimal,
  RateRegistry,
  SchemaNotFoundError,
  ToolUsage,
  attachTask,
  amendOutcome,
  explainPricing,
  getOutcomeHistory,
  instrumentOpenAI,
  instrumentOpenRouter,
  instrumentMongoClient,
  instrumentRedisClient,
  uninstrumentRedisClient,
  databaseResourceId,
  wrapRuntimeHandler,
  instrumentE2bSandbox,
  bindProviderBilling,
  instrumentLlamaParse,
  uninstrumentLlamaParse,
  instrumentObjectStorage,
  uninstrumentObjectStorage,
  cloudVectorResourceId,
  instrumentQdrant,
  uninstrumentQdrant,
  instrumentZilliz,
  uninstrumentZilliz,
  type CloudVectorBinding,
  recordOutcome,
  recordRevenue,
  trackTool,
  type CapabilityIdentity,
  type AmendOutcomeOptions,
  type TaskOptions,
  type ToolCallOptions,
  type InfrastructureRateEntry,
  type AttributionCapabilityKindV3,
  type AttributionCapabilitySourceV3,
  type AttributionCapabilityInvocationV3,
  type AttributionOperationErrorV3,
} from "@dexcost/sdk";
import { MongoClient } from "mongodb";
import { createClient } from "redis";

function checkDatabaseTypes(tracker: CostTracker): void {
  const config = { billingAccountId: "account", resourceId: "database" };
  const mongo = new MongoClient("mongodb://localhost:27017", { monitorCommands: true });
  const stop: () => void = instrumentMongoClient(mongo, tracker, config);
  const redis = instrumentRedisClient(createClient(), tracker, config);
  const pending: Promise<string | null> = redis.client.get("not-executed");
  const id: string = databaseResourceId(config.billingAccountId, config.resourceId);
  void pending; void id; stop(); uninstrumentRedisClient(redis);
}
void checkDatabaseTypes;

function checkWaveTwoNativeTypes(tracker: CostTracker): void {
  const parsing = {
    parsing: {
      parse: async (_options: { expand: string[] }) => ({ job: { id: "job" } }),
    },
  };
  const wrappedParse: typeof parsing = instrumentLlamaParse(parsing, tracker, {
    billingAccountId: "organization", projectId: "project",
  });
  const result: Promise<{ job: { id: string } }> = wrappedParse.parsing.parse({ expand: ["usage"] });
  const nativeStorage = { send: async (_command: { input: { Bucket: string } }) => ({ ETag: "etag" }) };
  const wrappedStorage: typeof nativeStorage = instrumentObjectStorage(nativeStorage, tracker, {
    provider: "aws_s3", billingAccountId: "123456789012", bucketOwnerAccountId: "123456789012",
    bucket: "example-bucket", region: "us-east-1", ownerPays: true,
  });
  const response: Promise<{ ETag: string }> = wrappedStorage.send({ input: { Bucket: "example-bucket" } });
  const stop: () => void = bindProviderBilling({}, {
    provider: "cohere", tier: "unknown", endpoint: "https://api.cohere.com",
  });
  void result; void response; stop();
  uninstrumentLlamaParse(wrappedParse); uninstrumentObjectStorage(wrappedStorage);
  // @ts-expect-error a billing tier is a constrained assertion, not an arbitrary string
  bindProviderBilling({}, { provider: "cohere", tier: "enterprise", endpoint: "https://api.cohere.com" });
}
void checkWaveTwoNativeTypes;

function checkCloudVectorTypes(tracker: CostTracker): void {
  const binding: CloudVectorBinding = {
    billingAccountId: "account", region: "us-east-1", clusterHost: "cluster.cloud.qdrant.io",
  };
  const client = { query: async (_collection: string, _query: { limit: number }) => ({ points: [] as string[] }) };
  const captured: typeof client = instrumentQdrant(client, tracker, binding);
  const result: Promise<{ points: string[] }> = captured.query("example", { limit: 1 });
  const zillizClient = { search: async (_request: { collection_name: string }) => ({ results: [] as string[] }) };
  const capturedZilliz: typeof zillizClient = instrumentZilliz(zillizClient, tracker, {
    ...binding, clusterHost: "cluster.serverless.us-east-1.vectordb.zillizcloud.com",
  });
  const search: Promise<{ results: string[] }> = capturedZilliz.search({ collection_name: "example" });
  const identity: string = cloudVectorResourceId("qdrant_cloud", binding.billingAccountId, binding.region, binding.clusterHost);
  void result; void search; void identity;
  uninstrumentQdrant(captured); uninstrumentZilliz(capturedZilliz);
  // @ts-expect-error provider is a bounded hosted vector provider, not an arbitrary service
  cloudVectorResourceId("unverified", binding.billingAccountId, binding.region, binding.clusterHost);
}
void checkCloudVectorTypes;
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
const amendOptions: AmendOutcomeOptions = { state: "missed", expectedRevision: 1 };

const tracker = new CostTracker({ autoInstrument: [], trackHttp: false });
const runtimeHandler: (input: string) => Promise<number> = wrapRuntimeHandler(async (input: string) => input.length,
  tracker, { serviceKey: "modal_compute", billingAccountId: "acct", resourceId: "app" });
const runtimeSync: (input: number) => number = wrapRuntimeHandler((input: number) => input + 1,
  tracker, { serviceKey: "modal_compute", billingAccountId: "acct", resourceId: "app" });
const sandboxCapture = instrumentE2bSandbox({ sandboxId: "sb-1", commands: { run: async (_command: string) => 1 } }, tracker, { billingAccountId: "acct" });
const runtimeResult: Promise<number> = sandboxCapture.sandbox.commands.run("test");
void runtimeHandler; void runtimeSync; void runtimeResult;
tracker.getCost("anthropic/claude", 100, 10, 20, 30, 40);
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
  amendOutcome,
  explainPricing,
  getOutcomeHistory,
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
void amendOptions;
void exactRate;
void publicValues;
