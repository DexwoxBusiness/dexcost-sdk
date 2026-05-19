package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/google/uuid"
)

// RecordGeminiResponse records an LLM cost event from a Google Gemini-style response.
//
// The response map must have the following structure:
//
//	{
//	  "model": "gemini-1.5-pro",
//	  "usageMetadata": {
//	    "promptTokenCount":     100,
//	    "candidatesTokenCount": 50,
//	    "cachedContentTokenCount": 20,  // optional
//	  }
//	}
//
// The event is inserted into buffer and the populated Event is returned.
func RecordGeminiResponse(
	buffer core.Buffer,
	pricingEngine *pricing.Engine,
	taskID uuid.UUID,
	response map[string]interface{},
) (core.Event, error) {
	model, ok := response["model"].(string)
	if !ok || model == "" {
		return core.Event{}, fmt.Errorf("clients: response missing string field \"model\"")
	}

	usage, ok := response["usageMetadata"].(map[string]interface{})
	if !ok {
		return core.Event{}, fmt.Errorf("clients: response missing map field \"usageMetadata\"")
	}

	promptTokens := intFromMap(usage, "promptTokenCount")
	candidatesTokens := intFromMap(usage, "candidatesTokenCount")
	cachedTokens := intFromMap(usage, "cachedContentTokenCount")

	costResult := pricingEngine.GetCost(model, promptTokens, candidatesTokens, cachedTokens, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "google"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(promptTokens)
	event.OutputTokens = intPtr(candidatesTokens)
	if cachedTokens > 0 {
		event.CachedTokens = intPtr(cachedTokens)
	}

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert gemini event: %w", err)
	}

	return event, nil
}
