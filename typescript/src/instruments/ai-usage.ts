/**
 * Vercel AI SDK usage extraction — shared by the module-level instrument
 * (`instruments/vercel-ai.ts`, effective on `ai` v4 CJS) and the
 * model-level middleware (`integrations/ai-sdk.ts`, the supported path on
 * `ai` >= 5 whose ESM exports cannot be patched).
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Normalized token counts extracted from an AI SDK usage object. */
export interface ExtractedUsage {
  /** Provider-normalized total input tokens, including cache buckets. */
  inputTokens: number;
  /** Provider-normalized total output tokens, including reasoning. */
  outputTokens: number;
  /** Mutually exclusive cache-read bucket. */
  cachedTokens: number;
  /** Mutually exclusive cache-write bucket. */
  cacheWriteTokens: number;
  /** Mutually exclusive non-cached input bucket. */
  uncachedInputTokens: number;
  /** Mutually exclusive ordinary/text output bucket. */
  textOutputTokens: number;
  /** Mutually exclusive reasoning output bucket. */
  reasoningTokens: number;
}

export function emptyExtractedUsage(): ExtractedUsage {
  return {
    inputTokens: 0,
    outputTokens: 0,
    cachedTokens: 0,
    cacheWriteTokens: 0,
    uncachedInputTokens: 0,
    textOutputTokens: 0,
    reasoningTokens: 0,
  };
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : undefined;
}

function normalizedBuckets(
  totalValue: unknown,
  regularValue: unknown,
  firstSpecialValue: unknown,
  secondSpecialValue: unknown,
): { total: number; regular: number; firstSpecial: number; secondSpecial: number } {
  let total = count(totalValue);
  let regular = count(regularValue);
  const firstSpecial = count(firstSpecialValue) ?? 0;
  const secondSpecial = count(secondSpecialValue) ?? 0;
  const specialTotal = firstSpecial + secondSpecial;

  if (regular === undefined) {
    if (total === undefined) regular = 0;
    else if (specialTotal <= total) regular = total - specialTotal;
    else {
      // AI SDK v5 exposed provider-dependent legacy shapes. In particular,
      // Anthropic could report uncached input separately while cachedInputTokens
      // exceeded inputTokens. Preserve both disjoint buckets instead of
      // clamping or double-counting them.
      regular = total;
      total += specialTotal;
    }
  }
  const represented = regular + specialTotal;
  if (total === undefined || total < represented) total = represented;
  return { total, regular, firstSpecial, secondSpecial };
}

/**
 * Extract token counts from a Vercel AI SDK usage object across major
 * versions. The field names were renamed between majors:
 *
 * - ai v4 (`LanguageModelUsage` / spec V1): `promptTokens` / `completionTokens`
 * - ai v5 (spec V2): `inputTokens` / `outputTokens`, cache reads in
 *   `cachedInputTokens`
 * - ai v6/v7 (spec V3/V4): `inputTokens` / `outputTokens`, cache reads in
 *   `inputTokenDetails.cacheReadTokens`
 *
 * Reading only the v4 names silently records 0 tokens (and therefore $0)
 * on every modern AI SDK install.
 */
export function extractUsage(usage: any): ExtractedUsage {
  if (!usage || typeof usage !== "object") {
    return emptyExtractedUsage();
  }

  // Low-level AI SDK v6/v7 middleware uses LanguageModelV3Usage, whose
  // inputTokens/outputTokens members are nested bucket objects. High-level
  // generateText/streamText exposes numeric totals plus *TokenDetails.
  const nestedInput = record(usage.inputTokens);
  const nestedOutput = record(usage.outputTokens);
  const inputDetails = record(usage.inputTokenDetails);
  const outputDetails = record(usage.outputTokenDetails);

  const input = nestedInput !== undefined
    ? normalizedBuckets(
        nestedInput.total,
        nestedInput.noCache,
        nestedInput.cacheRead,
        nestedInput.cacheWrite,
      )
    : normalizedBuckets(
        usage.inputTokens ?? usage.promptTokens,
        inputDetails?.noCacheTokens,
        inputDetails?.cacheReadTokens ?? usage.cachedInputTokens,
        inputDetails?.cacheWriteTokens,
      );
  const output = nestedOutput !== undefined
    ? normalizedBuckets(
        nestedOutput.total,
        nestedOutput.text,
        nestedOutput.reasoning,
        undefined,
      )
    : normalizedBuckets(
        usage.outputTokens ?? usage.completionTokens,
        outputDetails?.textTokens,
        outputDetails?.reasoningTokens ?? usage.reasoningTokens,
        undefined,
      );

  return {
    inputTokens: input.total,
    outputTokens: output.total,
    cachedTokens: input.firstSpecial,
    cacheWriteTokens: input.secondSpecial,
    uncachedInputTokens: input.regular,
    textOutputTokens: output.regular,
    reasoningTokens: output.firstSpecial,
  };
}

export function extractedUsageLines(
  usage: ExtractedUsage,
): Array<{ metric: string; quantity: string; unit: string }> {
  return [
    ["input_tokens", usage.uncachedInputTokens],
    ["cache_read_input_tokens", usage.cachedTokens],
    ["cache_write_input_tokens", usage.cacheWriteTokens],
    ["output_tokens", usage.textOutputTokens],
    ["reasoning_output_tokens", usage.reasoningTokens],
  ].flatMap(([metric, quantity]) => Number(quantity) > 0
    ? [{ metric: String(metric), quantity: String(quantity), unit: "Tokens" }]
    : []);
}
