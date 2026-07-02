/**
 * Vercel AI SDK usage extraction — shared by the module-level instrument
 * (`instruments/vercel-ai.ts`, effective on `ai` v4 CJS) and the
 * model-level middleware (`integrations/ai-sdk.ts`, the supported path on
 * `ai` >= 5 whose ESM exports cannot be patched).
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Normalized token counts extracted from an AI SDK usage object. */
export interface ExtractedUsage {
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
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
    return { inputTokens: 0, outputTokens: 0, cachedTokens: 0 };
  }
  const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    inputTokens: num(usage.inputTokens ?? usage.promptTokens),
    outputTokens: num(usage.outputTokens ?? usage.completionTokens),
    cachedTokens: num(
      usage.cachedInputTokens ?? usage.inputTokenDetails?.cacheReadTokens,
    ),
  };
}
