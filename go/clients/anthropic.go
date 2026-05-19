package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/google/uuid"
)

// RecordAnthropicResponse records an LLM cost event from an Anthropic-style response.
//
// The response map must have the following structure:
//
//	{
//	  "model": "claude-3-5-sonnet-20241022",
//	  "usage": {
//	    "input_tokens":            150,
//	    "output_tokens":            60,
//	    "cache_read_input_tokens":      30,  // optional
//	    "cache_creation_input_tokens":  20,  // optional
//	  }
//	}
//
// The event is inserted into buffer and the populated Event is returned.
func RecordAnthropicResponse(
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

	inputTokens := intFromMap(usage, "input_tokens")
	outputTokens := intFromMap(usage, "output_tokens")
	cacheReadTokens := intFromMap(usage, "cache_read_input_tokens")
	cacheCreationTokens := intFromMap(usage, "cache_creation_input_tokens")

	costResult := pricingEngine.GetCost(model, inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "anthropic"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(inputTokens)
	event.OutputTokens = intPtr(outputTokens)
	if cacheReadTokens > 0 {
		event.CachedTokens = intPtr(cacheReadTokens)
	}

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert anthropic event: %w", err)
	}

	return event, nil
}
