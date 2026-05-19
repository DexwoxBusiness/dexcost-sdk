package clients

import (
	"fmt"
	"strings"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/google/uuid"
)

// RecordBedrockResponse records an LLM cost event from an AWS Bedrock
// InvokeModel response. Bedrock fronts multiple model families (Anthropic,
// Amazon Titan, Meta Llama, Cohere, AI21, Mistral) and the response shape
// differs per family. Token extraction mirrors the Python helper at
// `instruments/bedrock.py:239`.
//
// The response map must look like:
//
//	{
//	  "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
//	  "body":    map[string]interface{}{ ... family-specific shape ... },
//	}
//
// Token extraction by family (matched against the lowercased modelId):
//   - "anthropic" / "claude":  body["usage"]["input_tokens"|"output_tokens"]
//   - "titan" / "amazon":      body["inputTextTokenCount"], body["results"][0]["tokenCount"]
//   - "meta" / "llama":        body["prompt_token_count"], body["generation_token_count"]
//   - "cohere":                body["token_count"]["input_tokens"|"output_tokens"]
//   - "ai21" / "jamba":        body["usage"]["prompt_tokens"|"completion_tokens"]
//   - generic fallback:        body["usage"]["input_tokens"|"prompt_tokens"|...]
//
// The generic fallback mirrors `instruments/bedrock.py:_extract_tokens` so the
// SDKs stay in lock-step for never-before-seen Bedrock model families.
//
// The provider prefix is stripped from modelId for storage
// (e.g. "anthropic.claude-3-5-sonnet" -> "claude-3-5-sonnet"); the full
// modelId is preserved in event.Details["bedrock_model_id"].
func RecordBedrockResponse(
	buffer core.Buffer,
	pricingEngine *pricing.Engine,
	taskID uuid.UUID,
	response map[string]interface{},
) (core.Event, error) {
	modelID, ok := response["modelId"].(string)
	if !ok || modelID == "" {
		return core.Event{}, fmt.Errorf("clients: response missing string field \"modelId\"")
	}

	body, _ := response["body"].(map[string]interface{})

	inputTokens, outputTokens := extractBedrockTokens(body, modelID)

	model := modelID
	if idx := strings.Index(model, "."); idx > 0 {
		model = model[idx+1:]
	}

	costResult := pricingEngine.GetCost(model, inputTokens, outputTokens, 0, 0)

	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "aws_bedrock"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion

	event.InputTokens = intPtr(inputTokens)
	event.OutputTokens = intPtr(outputTokens)
	event.Details["bedrock_model_id"] = modelID

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert bedrock event: %w", err)
	}
	return event, nil
}

// extractBedrockTokens picks (input, output) tokens from a Bedrock response
// body based on the model family encoded in modelID.
func extractBedrockTokens(body map[string]interface{}, modelID string) (int, int) {
	if body == nil {
		return 0, 0
	}
	model := strings.ToLower(modelID)

	switch {
	case strings.Contains(model, "anthropic"), strings.Contains(model, "claude"):
		if usage, ok := body["usage"].(map[string]interface{}); ok {
			return intFromMap(usage, "input_tokens"), intFromMap(usage, "output_tokens")
		}
	case strings.Contains(model, "titan"), strings.Contains(model, "amazon"):
		input := intFromMap(body, "inputTextTokenCount")
		output := 0
		if results, ok := body["results"].([]interface{}); ok && len(results) > 0 {
			if first, ok := results[0].(map[string]interface{}); ok {
				output = intFromMap(first, "tokenCount")
			}
		}
		return input, output
	case strings.Contains(model, "meta"), strings.Contains(model, "llama"):
		return intFromMap(body, "prompt_token_count"), intFromMap(body, "generation_token_count")
	case strings.Contains(model, "cohere"):
		if tc, ok := body["token_count"].(map[string]interface{}); ok {
			return intFromMap(tc, "input_tokens"), intFromMap(tc, "output_tokens")
		}
	case strings.Contains(model, "ai21"), strings.Contains(model, "jamba"):
		if usage, ok := body["usage"].(map[string]interface{}); ok {
			return intFromMap(usage, "prompt_tokens"), intFromMap(usage, "completion_tokens")
		}
	}

	// Generic fallback — try common patterns.
	if usage, ok := body["usage"].(map[string]interface{}); ok {
		input := intFromMap(usage, "input_tokens")
		if input == 0 {
			input = intFromMap(usage, "prompt_tokens")
		}
		output := intFromMap(usage, "output_tokens")
		if output == 0 {
			output = intFromMap(usage, "completion_tokens")
		}
		return input, output
	}
	return 0, 0
}
