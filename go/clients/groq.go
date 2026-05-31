package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/google/uuid"
)

// RecordGroqResponse records an LLM cost event from a Groq chat completion
// response. Groq's API is OpenAI-compatible, so the response shape mirrors
// `clients/openai.go`. The only difference is that this helper stamps
// `provider = "groq"`, so downstream queries can distinguish Groq usage from
// OpenAI's even though both speak the same wire format.
//
// The response map must look like:
//
//	{
//	  "model": "llama-3.1-70b-versatile",
//	  "usage": {
//	    "prompt_tokens":     100,
//	    "completion_tokens": 50,
//	  },
//	}
func RecordGroqResponse(
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

	costResult := pricingEngine.GetCost(model, promptTokens, completionTokens, 0, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "groq"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(promptTokens)
	event.OutputTokens = intPtr(completionTokens)

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert groq event: %w", err)
	}
	return event, nil
}
