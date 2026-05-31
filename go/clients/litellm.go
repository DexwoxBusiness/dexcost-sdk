package clients

import (
	"fmt"
	"strings"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/google/uuid"
)

// RecordLiteLLMResponse records an LLM cost event from a LiteLLM-style response.
//
// LiteLLM is a unified gateway across providers (OpenAI, Anthropic, Cohere,
// Bedrock, …). The response shape mirrors OpenAI's, plus optional
// "_hidden_params.custom_llm_provider" pointing at the actual upstream
// provider. The "model" field may carry a provider prefix (e.g. "openai/gpt-4o",
// "anthropic/claude-3-5-sonnet"); the raw model string is stored on the event
// unchanged — `pricing.Engine.findModel` strips the prefix internally for
// lookup. This matches the Python helper at `instruments/litellm.py:393`,
// which preserves response.model verbatim and only derives the provider
// for the event's `provider` field.
//
// The response map must look like:
//
//	{
//	  "model": "openai/gpt-4o",
//	  "usage": {
//	    "prompt_tokens":     100,
//	    "completion_tokens": 50,
//	  },
//	  "_hidden_params": {                 // optional
//	    "custom_llm_provider": "openai",  // overrides provider prefix
//	  },
//	}
//
// The event is inserted into buffer and the populated Event is returned.
func RecordLiteLLMResponse(
	buffer core.Buffer,
	pricingEngine *pricing.Engine,
	taskID uuid.UUID,
	response map[string]interface{},
) (core.Event, error) {
	rawModel, ok := response["model"].(string)
	if !ok || rawModel == "" {
		return core.Event{}, fmt.Errorf("clients: response missing string field \"model\"")
	}

	usage, ok := response["usage"].(map[string]interface{})
	if !ok {
		return core.Event{}, fmt.Errorf("clients: response missing map field \"usage\"")
	}

	provider := resolveLiteLLMProvider(rawModel, response)

	promptTokens := intFromMap(usage, "prompt_tokens")
	completionTokens := intFromMap(usage, "completion_tokens")

	costResult := pricingEngine.GetCost(rawModel, promptTokens, completionTokens, 0, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = provider
	event.Model = rawModel
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(promptTokens)
	event.OutputTokens = intPtr(completionTokens)

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert litellm event: %w", err)
	}
	return event, nil
}

// resolveLiteLLMProvider returns the provider name for a LiteLLM response.
// Resolution order matches `instruments/litellm.py:_resolve_provider`:
//  1. response["_hidden_params"]["custom_llm_provider"]
//  2. Prefix of the model string ("openai/gpt-4o" → "openai")
//  3. "unknown"
//
// Unlike a stripped-model approach, the raw model string stays on the event;
// the pricing engine handles the prefix internally.
func resolveLiteLLMProvider(rawModel string, response map[string]interface{}) string {
	if hp, ok := response["_hidden_params"].(map[string]interface{}); ok {
		if p, ok := hp["custom_llm_provider"].(string); ok && p != "" {
			return p
		}
	}
	if idx := strings.Index(rawModel, "/"); idx > 0 {
		return rawModel[:idx]
	}
	return "unknown"
}
