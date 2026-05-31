package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/google/uuid"
)

// RecordCohereResponse records an LLM cost event from a Cohere chat response.
//
// Cohere's chat API places token counts under ``meta.billed_units``. The
// response map must look like:
//
//	{
//	  "model": "command-r-plus",         // or pulled from request kwargs
//	  "meta": {
//	    "billed_units": {
//	      "input_tokens":  120,
//	      "output_tokens": 45,
//	    },
//	  },
//	}
//
// If "model" is absent, the helper falls back to "command-r-plus" — matching
// the Python instrument default at `instruments/cohere.py:245`.
func RecordCohereResponse(
	buffer core.Buffer,
	pricingEngine *pricing.Engine,
	taskID uuid.UUID,
	response map[string]interface{},
) (core.Event, error) {
	model, _ := response["model"].(string)
	if model == "" {
		model = "command-r-plus"
	}

	inputTokens, outputTokens := extractCohereTokens(response)

	costResult := pricingEngine.GetCost(model, inputTokens, outputTokens, 0, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "cohere"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(inputTokens)
	event.OutputTokens = intPtr(outputTokens)

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert cohere event: %w", err)
	}
	return event, nil
}

// extractCohereTokens reads input/output tokens from a Cohere chat response.
// Returns (0, 0) if the meta.billed_units sub-map is missing.
func extractCohereTokens(response map[string]interface{}) (int, int) {
	meta, ok := response["meta"].(map[string]interface{})
	if !ok {
		return 0, 0
	}
	billed, ok := meta["billed_units"].(map[string]interface{})
	if !ok {
		return 0, 0
	}
	return intFromMap(billed, "input_tokens"), intFromMap(billed, "output_tokens")
}
