import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { normalizeOpenAIUsage } from "../src/instruments/openai-usage.js";

interface ValidCase {
  id: string;
  usage: Record<string, unknown>;
  expected: Record<string, number>;
}

interface InvalidCase {
  id: string;
  usage: Record<string, unknown>;
  expected_error: string;
}

const fixture = JSON.parse(readFileSync(
  new URL("../../fixtures/openai_usage_conformance.json", import.meta.url),
  "utf8",
)) as { valid_cases: ValidCase[]; invalid_cases: InvalidCase[] };

function snakeCaseUsage(usage: ReturnType<typeof normalizeOpenAIUsage>): Record<string, number> {
  return {
    total_input_tokens: usage.totalInputTokens,
    input_tokens: usage.inputTokens,
    cache_read_input_tokens: usage.cacheReadInputTokens,
    cache_write_input_tokens: usage.cacheWriteInputTokens,
    total_output_tokens: usage.totalOutputTokens,
    output_tokens: usage.outputTokens,
    reasoning_output_tokens: usage.reasoningOutputTokens,
  };
}

describe("shared OpenAI usage conformance", () => {
  for (const testCase of fixture.valid_cases) {
    it(testCase.id, () => {
      expect(snakeCaseUsage(normalizeOpenAIUsage(testCase.usage))).toEqual(testCase.expected);
    });
  }

  for (const testCase of fixture.invalid_cases) {
    it(testCase.id, () => {
      expect(() => normalizeOpenAIUsage(testCase.usage)).toThrow(testCase.expected_error);
    });
  }

  it("keeps DeepSeek top-level cache-hit tokens disjoint", () => {
    expect(normalizeOpenAIUsage({
      prompt_tokens: 20,
      completion_tokens: 10,
      prompt_cache_hit_tokens: 4,
      prompt_cache_miss_tokens: 16,
    })).toEqual({
      totalInputTokens: 20,
      inputTokens: 16,
      cacheReadInputTokens: 4,
      cacheWriteInputTokens: 0,
      totalOutputTokens: 10,
      outputTokens: 10,
      reasoningOutputTokens: 0,
    });
  });
});
