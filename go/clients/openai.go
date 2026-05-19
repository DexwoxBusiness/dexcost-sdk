// Package clients provides helper functions for recording LLM cost events
// from OpenAI and Anthropic API responses. It does not import the upstream
// provider SDK packages; instead it accepts responses as map[string]interface{}
// and extracts fields via type assertions.
package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
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
//	    "cached_tokens":     20,   // optional
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

	promptTokens := intFromMap(usage, "prompt_tokens")
	completionTokens := intFromMap(usage, "completion_tokens")
	cachedTokens := intFromMap(usage, "cached_tokens")

	costResult := pricingEngine.GetCost(model, promptTokens, completionTokens, cachedTokens, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "openai"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(promptTokens)
	event.OutputTokens = intPtr(completionTokens)
	if cachedTokens > 0 {
		event.CachedTokens = intPtr(cachedTokens)
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
