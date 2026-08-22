/** Normalize OpenAI usage into mutually exclusive billing buckets. */

export interface NormalizedOpenAIUsage {
  totalInputTokens: number;
  inputTokens: number;
  cacheReadInputTokens: number;
  cacheWriteInputTokens: number;
  totalOutputTokens: number;
  outputTokens: number;
  reasoningOutputTokens: number;
}

export class OpenAIUsageError extends Error {}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function counter(value: unknown): number | undefined {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : undefined;
}

function optionalCounter(value: unknown): number {
  if (value === undefined || value === null) return 0;
  const parsed = counter(value);
  if (parsed === undefined) {
    throw new OpenAIUsageError("token counters must be non-negative integers");
  }
  return parsed;
}

export function normalizeOpenAIUsage(value: unknown): NormalizedOpenAIUsage {
  const usage = record(value);
  if (usage === undefined) {
    throw new OpenAIUsageError("usage is missing input or output token totals");
  }

  const rawInput = usage.prompt_tokens ?? usage.input_tokens;
  const rawOutput = usage.completion_tokens ?? usage.output_tokens;
  const totalInputTokens = counter(rawInput);
  const totalOutputTokens = counter(rawOutput);
  if (totalInputTokens === undefined || totalOutputTokens === undefined) {
    if (rawInput !== undefined && rawOutput !== undefined) {
      throw new OpenAIUsageError("token counters must be non-negative integers");
    }
    throw new OpenAIUsageError("usage is missing input or output token totals");
  }

  const inputDetails = record(usage.prompt_tokens_details ?? usage.input_tokens_details);
  const outputDetails = record(usage.completion_tokens_details ?? usage.output_tokens_details);
  const cacheReadInputTokens = optionalCounter(
    inputDetails?.cached_tokens ?? inputDetails?.cache_read_input_tokens ?? usage.cached_tokens,
  );
  const cacheWriteInputTokens = optionalCounter(
    inputDetails?.cache_write_tokens ?? inputDetails?.cache_creation_input_tokens,
  );
  const reasoningOutputTokens = optionalCounter(
    outputDetails?.reasoning_tokens ?? usage.reasoning_tokens,
  );

  if (cacheReadInputTokens + cacheWriteInputTokens > totalInputTokens) {
    throw new OpenAIUsageError("cache token buckets exceed total input tokens");
  }
  if (reasoningOutputTokens > totalOutputTokens) {
    throw new OpenAIUsageError("reasoning tokens exceed total output tokens");
  }

  return {
    totalInputTokens,
    inputTokens: totalInputTokens - cacheReadInputTokens - cacheWriteInputTokens,
    cacheReadInputTokens,
    cacheWriteInputTokens,
    totalOutputTokens,
    outputTokens: totalOutputTokens - reasoningOutputTokens,
    reasoningOutputTokens,
  };
}
