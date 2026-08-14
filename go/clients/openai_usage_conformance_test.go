package clients_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/clients"
)

type openAIUsageValidCase struct {
	ID       string                 `json:"id"`
	Usage    map[string]interface{} `json:"usage"`
	Expected map[string]int         `json:"expected"`
}

type openAIUsageInvalidCase struct {
	ID            string                 `json:"id"`
	Usage         map[string]interface{} `json:"usage"`
	ExpectedError string                 `json:"expected_error"`
}

func TestSharedOpenAIUsageConformance(t *testing.T) {
	raw, err := os.ReadFile("../../fixtures/openai_usage_conformance.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var fixture struct {
		ValidCases   []openAIUsageValidCase   `json:"valid_cases"`
		InvalidCases []openAIUsageInvalidCase `json:"invalid_cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}

	for _, testCase := range fixture.ValidCases {
		t.Run(testCase.ID, func(t *testing.T) {
			usage, err := clients.NormalizeOpenAIUsage(testCase.Usage)
			if err != nil {
				t.Fatalf("normalize: %v", err)
			}
			actual := map[string]int{
				"total_input_tokens":       usage.TotalInputTokens,
				"input_tokens":             usage.InputTokens,
				"cache_read_input_tokens":  usage.CacheReadInputTokens,
				"cache_write_input_tokens": usage.CacheWriteInputTokens,
				"total_output_tokens":      usage.TotalOutputTokens,
				"output_tokens":            usage.OutputTokens,
				"reasoning_output_tokens":  usage.ReasoningOutputTokens,
			}
			for key, expected := range testCase.Expected {
				if actual[key] != expected {
					t.Errorf("%s = %d, want %d", key, actual[key], expected)
				}
			}
		})
	}

	for _, testCase := range fixture.InvalidCases {
		t.Run(testCase.ID, func(t *testing.T) {
			_, err := clients.NormalizeOpenAIUsage(testCase.Usage)
			if err == nil || err.Error() != testCase.ExpectedError {
				t.Fatalf("error = %v, want %q", err, testCase.ExpectedError)
			}
		})
	}
}
