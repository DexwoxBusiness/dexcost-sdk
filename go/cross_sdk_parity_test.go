package dexcost

// Cross-SDK parity test for the Go SDK.
//
// Consumes the canonical fixture corpus at /home/user/dexcost-sdk/fixtures/
// (relative path: ../fixtures/) produced by python/tests/test_cross_sdk_parity.py.
// Asserts the Go SDK round-trips events / tasks and produces pricing output
// byte-equal (canonical_serialization) or decimal-equal (pricing) to the
// Python-canonical expected outputs.
//
// This test is intentionally RED on initial commit. Each failing sub-test
// pins an audit finding scheduled for Sprint 1+:
//   - B5  ec2 / k8s_pod compute discriminator
//   - B6  gpu_cost / gpu_utilization_signal schema enum gap
//   - P1  occurred_at timestamp format drift (Go RFC3339Nano vs Python +00:00)
//   - P2  PricingSource enum spelling drift
//   - URL scrubber absent in Go (Theme A, Sprint 1)
//
// As each finding is fixed the corresponding sub-test must flip green.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/DexwoxBusiness/dexcost-go/security"
	"github.com/shopspring/decimal"
)

var fixturesRoot = filepath.Join("..", "fixtures")

func readJSONFile(t *testing.T, path string) map[string]interface{} {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var out map[string]interface{}
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return out
}

func stripUnderscoredKeys(d map[string]interface{}) map[string]interface{} {
	cleaned := make(map[string]interface{}, len(d))
	for k, v := range d {
		if strings.HasPrefix(k, "_") {
			continue
		}
		if nested, ok := v.(map[string]interface{}); ok {
			cleaned[k] = stripUnderscoredKeys(nested)
		} else {
			cleaned[k] = v
		}
	}
	return cleaned
}

func normalizeViaJSON(t *testing.T, v interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]interface{}
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

func expectedPathFor(rel string, kind string) string {
	// Map fixture-input relative paths to their expected-output counterparts.
	//   events/foo.json                       -> canonical_serialization/foo.json
	//   events/edge_cases/foo.json            -> canonical_serialization/edge_cases/foo.json
	//   tasks/foo.json                        -> canonical_serialization/tasks/foo.json
	//   pricing_inputs/{compute|gpu|egress|llm}/foo.json -> pricing/{compute|gpu|egress|llm}/foo.json
	base := filepath.Base(rel)
	switch {
	case strings.HasPrefix(rel, "pricing_inputs/"):
		// strip "pricing_inputs/" prefix, keep subdir
		sub := strings.TrimPrefix(rel, "pricing_inputs/")
		return filepath.Join(fixturesRoot, "expected_outputs", kind, sub)
	case strings.Contains(rel, "edge_cases/"):
		return filepath.Join(fixturesRoot, "expected_outputs", kind, "edge_cases", base)
	case strings.HasPrefix(rel, "tasks/"):
		return filepath.Join(fixturesRoot, "expected_outputs", kind, "tasks", base)
	default:
		return filepath.Join(fixturesRoot, "expected_outputs", kind, base)
	}
}

var eventFixtures = []string{
	"events/llm_call.v1.json",
	"events/external_cost.v1.json",
	"events/compute_cost_lambda.v1.json",
	"events/compute_cost_ec2_share.v1.json",
	"events/compute_cost_k8s_pod.v1.json",
	"events/network.v1.json",
	"events/network_4xx_below_threshold.v1.json",
	"events/gpu_cost.v1.json",
	"events/gpu_utilization_signal.v1.json",
	"events/retry_marker.v1.json",
	"events/edge_cases/tiny_decimal.v1.json",
}

var taskFixtures = []string{
	"tasks/task_minimal.v1.json",
	"tasks/task_with_network_gpu.v1.json",
}

func TestCrossSDKEventCanonicalSerialization(t *testing.T) {
	for _, rel := range eventFixtures {
		rel := rel
		t.Run(rel, func(t *testing.T) {
			input := stripUnderscoredKeys(readJSONFile(t, filepath.Join(fixturesRoot, rel)))
			expected := readJSONFile(t, expectedPathFor(rel, "canonical_serialization"))

			evt, err := core.EventFromDict(input)
			if err != nil {
				t.Fatalf("EventFromDict(%s): %v", rel, err)
			}
			actual := normalizeViaJSON(t, evt.ToDict())

			if !reflect.DeepEqual(actual, expected) {
				actualJSON, _ := json.MarshalIndent(actual, "", "  ")
				expectedJSON, _ := json.MarshalIndent(expected, "", "  ")
				t.Errorf("event canonical-serialization drift for %s\n--- expected ---\n%s\n--- actual ---\n%s",
					rel, expectedJSON, actualJSON)
			}
		})
	}
}

func TestCrossSDKTaskCanonicalSerialization(t *testing.T) {
	for _, rel := range taskFixtures {
		rel := rel
		t.Run(rel, func(t *testing.T) {
			input := stripUnderscoredKeys(readJSONFile(t, filepath.Join(fixturesRoot, rel)))
			expected := readJSONFile(t, expectedPathFor(rel, "canonical_serialization"))

			task, err := core.TaskFromDict(input)
			if err != nil {
				t.Fatalf("TaskFromDict(%s): %v", rel, err)
			}
			actual := normalizeViaJSON(t, task.ToDict())

			if !reflect.DeepEqual(actual, expected) {
				actualJSON, _ := json.MarshalIndent(actual, "", "  ")
				expectedJSON, _ := json.MarshalIndent(expected, "", "  ")
				t.Errorf("task canonical-serialization drift for %s\n--- expected ---\n%s\n--- actual ---\n%s",
					rel, expectedJSON, actualJSON)
			}
		})
	}
}

func decimalEqual(t *testing.T, expected, actual interface{}, label string) {
	t.Helper()
	expStr, ok := expected.(string)
	if !ok {
		t.Fatalf("%s: expected value not a string: %T %v", label, expected, expected)
	}
	expDec, err := decimal.NewFromString(expStr)
	if err != nil {
		t.Fatalf("%s: parse expected %q: %v", label, expStr, err)
	}
	var actDec decimal.Decimal
	switch v := actual.(type) {
	case decimal.Decimal:
		actDec = v
	case string:
		actDec, err = decimal.NewFromString(v)
		if err != nil {
			t.Fatalf("%s: parse actual %q: %v", label, v, err)
		}
	default:
		t.Fatalf("%s: actual type unsupported: %T %v", label, actual, actual)
	}
	if !expDec.Equal(actDec) {
		t.Errorf("%s drift: expected=%s actual=%s", label, expDec.String(), actDec.String())
	}
}

func TestCrossSDKLLMPricingParity(t *testing.T) {
	engine, err := pricing.NewEngine()
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	for _, rel := range []string{
		"pricing_inputs/llm/gpt4o_500_in_200_out.json",
		"pricing_inputs/llm/claude_sonnet_streaming_2000_in_1500_out.json",
	} {
		rel := rel
		t.Run(rel, func(t *testing.T) {
			input := stripUnderscoredKeys(readJSONFile(t, filepath.Join(fixturesRoot, rel)))
			expected := readJSONFile(t, expectedPathFor(rel, "pricing"))

			model, _ := input["model"].(string)
			inTok := int(input["input_tokens"].(float64))
			outTok := int(input["output_tokens"].(float64))
			cached := 0
			if c, ok := input["cached_tokens"].(float64); ok {
				cached = int(c)
			}

			result := engine.GetCost(model, inTok, outTok, cached, 0)

			decimalEqual(t, expected["cost_usd"], result.CostUSD, rel+":cost_usd")
			if got, want := result.PricingSource, expected["pricing_source"]; got != want {
				t.Errorf("%s pricing_source drift: expected=%v actual=%v", rel, want, got)
			}
		})
	}
}

func TestCrossSDKURLScrubberParity(t *testing.T) {
	// Sprint 1 / Theme A. Compares security.ScrubURL byte-for-byte against
	// expected_outputs/security/url_with_*.v1.json (Python-canonical).
	urlFixtures := []string{
		"events/edge_cases/url_with_basic_auth.v1.json",
		"events/edge_cases/url_with_api_key_query.v1.json",
		"events/edge_cases/url_with_signed_s3.v1.json",
	}
	for _, rel := range urlFixtures {
		rel := rel
		t.Run(rel, func(t *testing.T) {
			input := readJSONFile(t, filepath.Join(fixturesRoot, rel))
			testInput, ok := input["_test_input"].(map[string]interface{})
			if !ok {
				t.Fatalf("%s: missing _test_input", rel)
			}
			rawURL, _ := testInput["url"].(string)
			expected := readJSONFile(t, filepath.Join(fixturesRoot,
				"expected_outputs", "security", filepath.Base(rel)))

			actualScrubbed := security.ScrubURL(rawURL)
			expectedScrubbed, _ := expected["scrubbed_url"].(string)
			expectedRaw, _ := expected["raw_url"].(string)
			if expectedRaw != rawURL {
				t.Errorf("%s: raw_url mismatch fixture=%q expected=%q", rel, rawURL, expectedRaw)
			}
			if actualScrubbed != expectedScrubbed {
				t.Errorf("%s: scrub drift\n  raw:      %s\n  expected: %s\n  actual:   %s",
					rel, rawURL, expectedScrubbed, actualScrubbed)
			}
		})
	}
}

// TestCrossSDKTinyDecimalAccumulation pins the B3 invariant:
// summing 1.23E-8 ten thousand times must equal 0.0001230000 exactly.
func TestCrossSDKTinyDecimalAccumulation(t *testing.T) {
	expected := readJSONFile(t, filepath.Join(fixturesRoot, "expected_outputs", "pricing", "decimal_accumulation_invariant.json"))
	per := decimal.RequireFromString(expected["per_event_cost_usd"].(string))
	iters := int(expected["iterations"].(float64))
	wantTotal := decimal.RequireFromString(expected["total_cost_usd"].(string))

	var total decimal.Decimal
	for i := 0; i < iters; i++ {
		total = total.Add(per)
	}
	if !total.Equal(wantTotal) {
		t.Errorf("decimal accumulation drift: %d * %s = %s, expected %s",
			iters, per.String(), total.String(), wantTotal.String())
	}
}
