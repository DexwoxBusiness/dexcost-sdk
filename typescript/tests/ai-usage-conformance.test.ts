import { describe, expect, it } from "vitest";
import {
  extractedUsageLines,
  extractUsage,
} from "../src/instruments/ai-usage.js";

describe("Vercel AI SDK usage normalization", () => {
  it("normalizes AI SDK v6/v7 high-level totals into disjoint meters", () => {
    const usage = extractUsage({
      inputTokens: 2_600,
      inputTokenDetails: {
        noCacheTokens: 200,
        cacheReadTokens: 2_000,
        cacheWriteTokens: 400,
      },
      outputTokens: 300,
      outputTokenDetails: { textTokens: 180, reasoningTokens: 120 },
    });

    expect(usage).toEqual({
      inputTokens: 2_600,
      outputTokens: 300,
      cachedTokens: 2_000,
      cacheWriteTokens: 400,
      uncachedInputTokens: 200,
      textOutputTokens: 180,
      reasoningTokens: 120,
    });
    expect(extractedUsageLines(usage).map(({ metric, quantity }) => ({ metric, quantity }))).toEqual([
      { metric: "input_tokens", quantity: "200" },
      { metric: "cache_read_input_tokens", quantity: "2000" },
      { metric: "cache_write_input_tokens", quantity: "400" },
      { metric: "output_tokens", quantity: "180" },
      { metric: "reasoning_output_tokens", quantity: "120" },
    ]);
  });

  it("normalizes the low-level LanguageModelV3Usage shape used by middleware", () => {
    expect(extractUsage({
      inputTokens: { total: 2_302, noCache: 18, cacheRead: 2_284, cacheWrite: 0 },
      outputTokens: { total: 158, text: 140, reasoning: 18 },
    })).toEqual({
      inputTokens: 2_302,
      outputTokens: 158,
      cachedTokens: 2_284,
      cacheWriteTokens: 0,
      uncachedInputTokens: 18,
      textOutputTokens: 140,
      reasoningTokens: 18,
    });
  });

  it("keeps AI SDK v4 usage as ordinary input and output", () => {
    expect(extractUsage({ promptTokens: 25, completionTokens: 9 })).toEqual({
      inputTokens: 25,
      outputTokens: 9,
      cachedTokens: 0,
      cacheWriteTokens: 0,
      uncachedInputTokens: 25,
      textOutputTokens: 9,
      reasoningTokens: 0,
    });
  });

  it("handles the documented AI SDK v5 Anthropic separate-cache shape without loss", () => {
    expect(extractUsage({
      inputTokens: 18,
      cachedInputTokens: 2_284,
      outputTokens: 141,
    })).toEqual({
      inputTokens: 2_302,
      outputTokens: 141,
      cachedTokens: 2_284,
      cacheWriteTokens: 0,
      uncachedInputTokens: 18,
      textOutputTokens: 141,
      reasoningTokens: 0,
    });
  });

  it("rejects unsafe provider counters without throwing into application code", () => {
    expect(extractUsage({
      inputTokens: -1,
      outputTokens: 1.5,
      cachedInputTokens: Number.POSITIVE_INFINITY,
    })).toEqual({
      inputTokens: 0,
      outputTokens: 0,
      cachedTokens: 0,
      cacheWriteTokens: 0,
      uncachedInputTokens: 0,
      textOutputTokens: 0,
      reasoningTokens: 0,
    });
  });
});
