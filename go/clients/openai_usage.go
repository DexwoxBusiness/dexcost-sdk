package clients

import "fmt"

// OpenAIUsage contains provider totals and the disjoint DexCost billing buckets.
type OpenAIUsage struct {
	TotalInputTokens      int
	InputTokens           int
	CacheReadInputTokens  int
	CacheWriteInputTokens int
	TotalOutputTokens     int
	OutputTokens          int
	ReasoningOutputTokens int
}

func mapValue(value interface{}) map[string]interface{} {
	if result, ok := value.(map[string]interface{}); ok {
		return result
	}
	return nil
}

func tokenCounter(value interface{}) (int, bool) {
	switch number := value.(type) {
	case int:
		return number, number >= 0
	case int32:
		return int(number), number >= 0
	case int64:
		return int(number), number >= 0 && int64(int(number)) == number
	case float64:
		converted := int(number)
		return converted, number >= 0 && float64(converted) == number
	default:
		return 0, false
	}
}

func optionalTokenCounter(value interface{}) (int, error) {
	if value == nil {
		return 0, nil
	}
	parsed, ok := tokenCounter(value)
	if !ok {
		return 0, fmt.Errorf("token counters must be non-negative integers")
	}
	return parsed, nil
}

// NormalizeOpenAIUsage accepts Chat Completions or Responses API usage.
func NormalizeOpenAIUsage(usage map[string]interface{}) (OpenAIUsage, error) {
	rawInput, hasInput := usage["prompt_tokens"]
	if !hasInput {
		rawInput, hasInput = usage["input_tokens"]
	}
	rawOutput, hasOutput := usage["completion_tokens"]
	if !hasOutput {
		rawOutput, hasOutput = usage["output_tokens"]
	}
	if !hasInput || !hasOutput {
		return OpenAIUsage{}, fmt.Errorf("usage is missing input or output token totals")
	}
	totalInput, inputOK := tokenCounter(rawInput)
	totalOutput, outputOK := tokenCounter(rawOutput)
	if !inputOK || !outputOK {
		return OpenAIUsage{}, fmt.Errorf("token counters must be non-negative integers")
	}

	inputDetails := mapValue(usage["prompt_tokens_details"])
	if inputDetails == nil {
		inputDetails = mapValue(usage["input_tokens_details"])
	}
	outputDetails := mapValue(usage["completion_tokens_details"])
	if outputDetails == nil {
		outputDetails = mapValue(usage["output_tokens_details"])
	}

	var cachedValue interface{}
	if inputDetails != nil {
		cachedValue = inputDetails["cached_tokens"]
	} else {
		cachedValue = usage["cached_tokens"]
	}
	cached, err := optionalTokenCounter(cachedValue)
	if err != nil {
		return OpenAIUsage{}, err
	}
	var cacheWriteValue interface{}
	if inputDetails != nil {
		cacheWriteValue = inputDetails["cache_write_tokens"]
	}
	cacheWrite, err := optionalTokenCounter(cacheWriteValue)
	if err != nil {
		return OpenAIUsage{}, err
	}
	var reasoningValue interface{}
	if outputDetails != nil {
		reasoningValue = outputDetails["reasoning_tokens"]
	}
	reasoning, err := optionalTokenCounter(reasoningValue)
	if err != nil {
		return OpenAIUsage{}, err
	}

	if cached+cacheWrite > totalInput {
		return OpenAIUsage{}, fmt.Errorf("cache token buckets exceed total input tokens")
	}
	if reasoning > totalOutput {
		return OpenAIUsage{}, fmt.Errorf("reasoning tokens exceed total output tokens")
	}

	return OpenAIUsage{
		TotalInputTokens:      totalInput,
		InputTokens:           totalInput - cached - cacheWrite,
		CacheReadInputTokens:  cached,
		CacheWriteInputTokens: cacheWrite,
		TotalOutputTokens:     totalOutput,
		OutputTokens:          totalOutput - reasoning,
		ReasoningOutputTokens: reasoning,
	}, nil
}
