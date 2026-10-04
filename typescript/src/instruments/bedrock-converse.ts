import type { OperationMeasurement } from "./provider-metering.js";

/* eslint-disable @typescript-eslint/no-explicit-any */
export const NOVA_MODELS = new Set(["amazon.nova-micro-v1:0", "amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"]);
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const textBlocks = (blocks: any): boolean => Array.isArray(blocks) && blocks.every((block) =>
  block && Object.keys(block).length === 1 && typeof block.text === "string");

/** Snapshot only route/configuration facts, never credentials or prompt contents. */
export async function novaRequestEligible(client: any, input: any): Promise<boolean> {
  try {
    const config = client?.config;
    const region = typeof config?.region === "function" ? await config.region() : config?.region;
    // Custom endpoint resolvers and endpoint overrides cannot inherit AWS public rates.
    if (region !== "us-east-1" || config.endpoint !== undefined ||
        config.endpointProvider?.name !== "defaultEndpointResolver" ||
        await config.useFipsEndpoint?.() || await config.useDualstackEndpoint?.()) return false;
    return novaInputEligible(input);
  } catch { return false; }
}

export function novaInputEligible(input: any): boolean {
  return NOVA_MODELS.has(input?.modelId) &&
    (input.serviceTier == null || input.serviceTier.type === "default") &&
    (input.performanceConfig == null || input.performanceConfig.latency === "standard") &&
    input.guardrailConfig == null && input.additionalModelRequestFields == null &&
    input.promptVariables == null && input.toolConfig == null &&
    Array.isArray(input.messages) && input.messages.length > 0 &&
    input.messages.every((message: any) => textBlocks(message?.content)) &&
    (input.system == null || textBlocks(input.system));
}

export function novaMeasurement(input: any, response: any, eligible: boolean): OperationMeasurement {
  const usage = response?.usage;
  const valid = count(usage?.inputTokens) && count(usage?.outputTokens) && count(usage?.totalTokens) &&
    usage.totalTokens === usage.inputTokens + usage.outputTokens;
  const noCache = [usage?.cacheReadInputTokens, usage?.cacheWriteInputTokens].every((v) => v == null || v === 0) &&
    (usage?.cacheDetails == null || (Array.isArray(usage.cacheDetails) && usage.cacheDetails.length === 0));
  const priced = eligible && valid && noCache && response?.serviceTier?.type === "default" &&
    response?.performanceConfig?.latency === "standard" && response?.trace == null &&
    ["end_turn", "max_tokens", "stop_sequence"].includes(response?.stopReason);
  return {
    usageLines: valid ? [
      { metric: "input_tokens", quantity: usage.inputTokens, unit: "Tokens" },
      { metric: "output_tokens", quantity: usage.outputTokens, unit: "Tokens" },
    ] : [],
    pricingUsage: {}, responseModel: input?.modelId,
    providerRecordId: response?.$metadata?.requestId,
    inputTokens: valid ? usage.inputTokens : undefined, outputTokens: valid ? usage.outputTokens : undefined,
    billingDimensions: priced ? [["bedrock_pricing_lane", "us_east_1_nova_standard_no_cache"]] : [],
  };
}

export class NovaStreamMeter {
  private metadata: any;
  private stopReason: string | undefined;
  private invalid = false;
  constructor(private input: any, private eligible: boolean, private requestId?: string) {}
  observe(value: any): void {
    if (!value || typeof value !== "object" || Object.keys(value).some((key) => /exception$/i.test(key))) this.invalid = true;
    if (value?.messageStop) {
      if (this.stopReason !== undefined) this.invalid = true;
      this.stopReason = value.messageStop.stopReason;
    }
    if (value?.metadata) {
      if (this.metadata !== undefined || this.stopReason === undefined) this.invalid = true;
      this.metadata = value.metadata;
    }
  }
  measurement(): OperationMeasurement {
    return novaMeasurement(this.input, { ...this.metadata, stopReason: this.stopReason,
      $metadata: { requestId: this.requestId } }, this.eligible && !this.invalid);
  }
  status(): "succeeded" | "unknown" {
    return this.metadata && this.stopReason && !this.invalid ? "succeeded" : "unknown";
  }
}
