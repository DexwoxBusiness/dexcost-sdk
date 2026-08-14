// Package clients provides helper functions for recording LLM cost events
// from OpenAI and Anthropic API responses. It does not import the upstream
// provider SDK packages; instead it accepts responses as map[string]interface{}
// and extracts fields via type assertions.
package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/google/uuid"
)

// RecordOpenAIResponse records an LLM cost event from an OpenAI-style response.
//
// The response map must have the following structure:
//
//	{
//	  "model": "gpt-4o",
//	  "usage": {
//	    "prompt_tokens":     100,
//	    "completion_tokens": 50,
//	    "prompt_tokens_details": {
//	      "cached_tokens": 20,
//	      "cache_write_tokens": 10,
//	    },
//	  }
//	}
//
// The event is inserted into buffer and the populated Event is returned.
func RecordOpenAIResponse(
	buffer core.Buffer,
	pricingEngine *pricing.Engine,
	taskID uuid.UUID,
	response map[string]interface{},
) (core.Event, error) {
	model, ok := response["model"].(string)
	if !ok || model == "" {
		return core.Event{}, fmt.Errorf("clients: response missing string field \"model\"")
	}

	usage, ok := response["usage"].(map[string]interface{})
	if !ok {
		return core.Event{}, fmt.Errorf("clients: response missing map field \"usage\"")
	}

	normalized, err := NormalizeOpenAIUsage(usage)
	if err != nil {
		return core.Event{}, fmt.Errorf("clients: invalid openai usage: %w", err)
	}

	costResult := pricingEngine.GetCost(
		model,
		normalized.TotalInputTokens,
		normalized.TotalOutputTokens,
		normalized.CacheReadInputTokens,
		normalized.CacheWriteInputTokens,
	)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "openai"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(normalized.TotalInputTokens)
	event.OutputTokens = intPtr(normalized.TotalOutputTokens)
	if normalized.CacheReadInputTokens > 0 {
		event.CachedTokens = intPtr(normalized.CacheReadInputTokens)
	}
	if normalized.CacheWriteInputTokens > 0 {
		event.Details["cache_write_input_tokens"] = normalized.CacheWriteInputTokens
	}
	if normalized.ReasoningOutputTokens > 0 {
		event.Details["reasoning_output_tokens"] = normalized.ReasoningOutputTokens
	}
	if recordID, ok := response["id"].(string); ok && recordID != "" {
		event.Details["provider_record_id"] = recordID
	}

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert openai event: %w", err)
	}

	return event, nil
}

// intFromMap extracts an int from a map[string]interface{} by key.
// Returns 0 if the key is absent or the value is an unrecognised type.
func intFromMap(m map[string]interface{}, key string) int {
	v, ok := m[key]
	if !ok {
		return 0
	}
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	case int64:
		return int(n)
	default:
		return 0
	}
}

// intPtr returns a pointer to an int value.
func intPtr(v int) *int {
	return &v
}
