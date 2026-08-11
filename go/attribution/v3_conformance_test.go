package attribution

import (
	"encoding/json"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

type v3ValidCase struct {
	ID    string                 `json:"id"`
	Event map[string]interface{} `json:"event"`
}

type v3InvalidCase struct {
	ID              string                 `json:"id"`
	ExpectedPath    string                 `json:"expected_error_path"`
	Event           map[string]interface{} `json:"event"`
	MutateFrom      string                 `json:"mutate_from"`
	Set             map[string]interface{} `json:"set"`
	Delete          []string               `json:"delete"`
	AppendUsage     interface{}            `json:"append_usage"`
	AppendDimension interface{}            `json:"append_dimension"`
}

func loadV3Corpus(t *testing.T) (string, []v3ValidCase, []v3InvalidCase) {
	t.Helper()
	raw, err := os.ReadFile("../../fixtures/attribution_v3/conformance.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct {
		Version string          `json:"observation_contract_version"`
		Valid   []v3ValidCase   `json:"valid_observations"`
		Invalid []v3InvalidCase `json:"invalid_observations"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	return corpus.Version, corpus.Valid, corpus.Invalid
}

func cloneV3Map(t *testing.T, value map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var clone map[string]interface{}
	if err := json.Unmarshal(raw, &clone); err != nil {
		t.Fatal(err)
	}
	return clone
}

func v3ParentAndKey(t *testing.T, event map[string]interface{}, path string) (interface{}, string) {
	t.Helper()
	parts := strings.Split(path, ".")
	var parent interface{} = event
	for _, part := range parts[:len(parts)-1] {
		switch current := parent.(type) {
		case map[string]interface{}:
			parent = current[part]
		case []interface{}:
			index, err := strconv.Atoi(part)
			if err != nil || index < 0 || index >= len(current) {
				t.Fatalf("invalid corpus path %q", path)
			}
			parent = current[index]
		default:
			t.Fatalf("invalid corpus parent at %q", path)
		}
	}
	return parent, parts[len(parts)-1]
}

func materializeV3Invalid(t *testing.T, testCase v3InvalidCase, validByID map[string]map[string]interface{}) map[string]interface{} {
	t.Helper()
	if testCase.Event != nil {
		return cloneV3Map(t, testCase.Event)
	}
	base, ok := validByID[testCase.MutateFrom]
	if !ok {
		t.Fatalf("unknown corpus base %q", testCase.MutateFrom)
	}
	event := cloneV3Map(t, base)
	for path, value := range testCase.Set {
		parent, key := v3ParentAndKey(t, event, path)
		switch current := parent.(type) {
		case map[string]interface{}:
			current[key] = value
		case []interface{}:
			index, err := strconv.Atoi(key)
			if err != nil || index < 0 || index >= len(current) {
				t.Fatalf("invalid corpus set path %q", path)
			}
			current[index] = value
		}
	}
	for _, path := range testCase.Delete {
		parent, key := v3ParentAndKey(t, event, path)
		if current, ok := parent.(map[string]interface{}); ok {
			delete(current, key)
		}
	}
	if testCase.AppendUsage != nil {
		event["usage"] = append(event["usage"].([]interface{}), testCase.AppendUsage)
	}
	if testCase.AppendDimension != nil {
		line := event["usage"].([]interface{})[0].(map[string]interface{})
		line["dimensions"] = append(line["dimensions"].([]interface{}), testCase.AppendDimension)
	}
	return event
}

func TestSharedAttributionV3Conformance(t *testing.T) {
	version, valid, invalid := loadV3Corpus(t)
	if version != ContractVersionV3 {
		t.Fatalf("fixture version %s != %s", version, ContractVersionV3)
	}
	canonical, err := os.ReadFile("../../fixtures/attribution_v3/schemas.json")
	if err != nil {
		t.Fatal(err)
	}
	packaged, err := os.ReadFile("attribution-v3-schema.json")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(packaged, canonical) {
		t.Fatal("packaged attribution v3 schema differs from the authoritative fixture")
	}
	validByID := make(map[string]map[string]interface{}, len(valid))
	for _, testCase := range valid {
		validByID[testCase.ID] = testCase.Event
		t.Run("valid/"+testCase.ID, func(t *testing.T) {
			result := ValidateObservationV3(testCase.Event)
			if !result.Success || len(result.Issues) != 0 {
				t.Fatalf("unexpected issues: %+v", result.Issues)
			}
		})
	}
	for _, testCase := range invalid {
		t.Run("invalid/"+testCase.ID, func(t *testing.T) {
			result := ValidateObservationV3(materializeV3Invalid(t, testCase, validByID))
			if result.Success {
				t.Fatal("expected validation failure")
			}
			for _, issue := range result.Issues {
				if issue.Path == testCase.ExpectedPath {
					return
				}
			}
			t.Fatalf("missing expected path %q in %+v", testCase.ExpectedPath, result.Issues)
		})
	}
}

func v3TestEvent(eventType core.EventType) core.Event {
	event := core.NewEvent(uuid.MustParse("22222222-2222-4222-8222-222222222222"), eventType)
	event.EventID = uuid.MustParse("11111111-1111-4111-8111-111111111111")
	event.OccurredAt = time.Date(2026, 8, 11, 10, 0, 0, 123_000_000, time.UTC)
	return event
}

func TestValidateObservationV3RejectsNonStringIdentitiesWithoutPanicking(t *testing.T) {
	_, valid, _ := loadV3Corpus(t)
	if len(valid) == 0 {
		t.Fatal("shared v3 corpus has no valid observation")
	}

	tests := []struct {
		name   string
		mutate func(map[string]interface{})
	}{
		{
			name: "usage line ID object",
			mutate: func(event map[string]interface{}) {
				line := event["usage"].([]interface{})[0].(map[string]interface{})
				line["line_id"] = map[string]interface{}{"invalid": true}
			},
		},
		{
			name: "dimension key array",
			mutate: func(event map[string]interface{}) {
				line := event["usage"].([]interface{})[0].(map[string]interface{})
				line["dimensions"] = []interface{}{map[string]interface{}{
					"key": []interface{}{"invalid"},
					"value": map[string]interface{}{
						"type":  "string",
						"value": "safe",
					},
				}}
			},
		},
		{
			name: "attempt identity arrays",
			mutate: func(event map[string]interface{}) {
				attempt := event["operation"].(map[string]interface{})["attempt"].(map[string]interface{})
				attempt["id"] = []interface{}{"invalid"}
				attempt["retry_of"] = []interface{}{"invalid"}
			},
		},
	}

	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			event := cloneV3Map(t, valid[0].Event)
			testCase.mutate(event)
			if result := ValidateObservationV3(event); result.Success {
				t.Fatal("expected malformed identity to fail validation")
			}
		})
	}
}

func TestToObservationV3StableUsageAndOperationIdentities(t *testing.T) {
	event := v3TestEvent(core.EventTypeLLMCall)
	event.Provider = "anthropic"
	event.Model = "claude-sonnet-4-5"
	input, cached, output := 100, 1_000, 50
	event.InputTokens, event.CachedTokens, event.OutputTokens = &input, &cached, &output
	event.CostUSD = decimal.RequireFromString("0.00135")
	event.CostConfidence = core.CostConfidenceExact
	event.PricingSource = core.PricingSourceServiceCatalog
	event.PricingVersion = "llm:2026-08-11"
	event.Details["cache_creation_input_tokens"] = 25
	first, second := ToObservationV3(event), ToObservationV3(event)
	if first == nil || second == nil || !reflect.DeepEqual(first, second) {
		t.Fatalf("conversion is nil or unstable: %+v %+v", first, second)
	}
	if first.SchemaVersion != "3" || first.UsageSnapshot != "full" || first.Operation.ID != event.EventID.String() || first.Operation.Attempt.Number != 1 {
		t.Fatalf("invalid operation identity: %+v", first)
	}
	if len(first.Usage) != 4 {
		t.Fatalf("usage lines = %d, want 4: %+v", len(first.Usage), first.Usage)
	}
	lineIDs := make(map[string]struct{}, len(first.Usage))
	for _, line := range first.Usage {
		lineIDs[line.LineID] = struct{}{}
		if len(line.Dimensions) != 0 {
			t.Fatalf("unexpected dimensions: %+v", line.Dimensions)
		}
	}
	if len(lineIDs) != len(first.Usage) {
		t.Fatal("usage line IDs are not unique")
	}
	if first.CostEvidence == nil || first.CostEvidence.Source != "sdk_catalog" || first.CostEvidence.Confidence != "computed" {
		t.Fatalf("invalid cost evidence: %+v", first.CostEvidence)
	}
}

func TestToObservationV3DoesNotInventComputeUsage(t *testing.T) {
	if converted := ToObservationV3(v3TestEvent(core.EventTypeComputeCost)); converted != nil {
		t.Fatalf("successful compute without observed usage must be rejected: %+v", converted)
	}
}

func TestToObservationV3RetryLineageAndUnknownMeter(t *testing.T) {
	retry := v3TestEvent(core.EventTypeRetryMarker)
	retry.EventID = uuid.New()
	retryOf := uuid.New()
	retry.IsRetry, retry.RetryReason, retry.RetryOf = true, "rate_limit", &retryOf
	retry.CostUSD = decimal.RequireFromString("0.02")
	retry.Details["attribution_operation_id"] = retryOf.String()
	retry.Details["attribution_attempt_number"] = 2
	converted := ToObservationV3(retry)
	if converted == nil || converted.Operation.ID != retryOf.String() || converted.Operation.Attempt.Number != 2 || converted.Operation.Attempt.RetryOf != retryOf.String() {
		t.Fatalf("retry lineage was not nested correctly: %+v", converted)
	}
	if converted.Resource == nil || converted.Resource.ID != "rate_limit" {
		t.Fatalf("retry reason resource lost: %+v", converted.Resource)
	}

	unknown := v3TestEvent(core.EventTypeExternalCost)
	unknown.ServiceName = "future-provider"
	unknown.Details["attribution_component"] = "telephony"
	unknown.Details["attribution_usage_metric"] = "provider_new_meter"
	unknown.Details["attribution_usage_unit"] = "Widgets"
	unknown.Details["attribution_usage_quantity"] = "7.5"
	unknown.Details["attribution_dimensions"] = []interface{}{map[string]interface{}{
		"key": "priority", "value": map[string]interface{}{"type": "string", "value": "fast"},
	}}
	observed := ToObservationV3(unknown)
	if observed == nil || observed.Component != ComponentTelephony || len(observed.Usage) != 1 || observed.Usage[0].Metric != "provider_new_meter" || observed.CostEvidence != nil {
		t.Fatalf("unknown meter was not retained as visibly unpriced: %+v", observed)
	}
	if observed.Usage[0].LineID != "f27292e7-f1a6-5aa0-9ebf-9a02595093dd" {
		t.Fatalf("usage line ID drifted across SDKs: %s", observed.Usage[0].LineID)
	}
}

func TestToObservationV3RetainsGPUSignalAsUsageOnly(t *testing.T) {
	event := v3TestEvent(core.EventTypeGPUUtilizationSignal)
	event.Details["gpu_index"] = 0
	event.Details["gpu_sku"] = "h100"
	event.Details["sm_util_pct"] = 42.5
	event.Details["vram_used_peak_bytes"] = 1024
	event.Details["task_duration_ms"] = 60_000
	converted := ToObservationV3(event)
	if converted == nil || converted.Component != ComponentGPU || converted.Operation.Status != "unknown" || converted.CostEvidence != nil {
		t.Fatalf("invalid GPU signal conversion: %+v", converted)
	}
	if converted.UsagePeriod == nil || converted.UsagePeriod.StartAt != "2026-08-11T09:59:00.123000Z" || converted.UsagePeriod.EndAt != "2026-08-11T10:00:00.123000Z" {
		t.Fatalf("invalid GPU signal period: %+v", converted.UsagePeriod)
	}
	metrics := []string{converted.Usage[0].Metric, converted.Usage[1].Metric}
	if !reflect.DeepEqual(metrics, []string{"gpu.sm_utilization_percent", "gpu.vram_peak_bytes"}) {
		t.Fatalf("GPU metrics = %+v", metrics)
	}
}

func TestToObservationV3CountsDimensionStringCharacters(t *testing.T) {
	event := v3TestEvent(core.EventTypeExternalCost)
	event.ServiceName = "future-provider"
	event.Details["attribution_component"] = "external"
	event.Details["attribution_usage_metric"] = "provider_new_meter"
	event.Details["attribution_usage_unit"] = "Widgets"
	event.Details["attribution_usage_quantity"] = "1"
	event.Details["attribution_dimensions"] = []interface{}{map[string]interface{}{
		"key": "label",
		"value": map[string]interface{}{
			"type":  "string",
			"value": strings.Repeat("界", 256),
		},
	}}

	if converted := ToObservationV3(event); converted == nil {
		t.Fatal("256 Unicode characters must satisfy the schema maxLength")
	}

	event.Details["attribution_dimensions"].([]interface{})[0].(map[string]interface{})["value"].(map[string]interface{})["value"] = strings.Repeat("界", 257)
	if converted := ToObservationV3(event); converted != nil {
		t.Fatal("257 Unicode characters must exceed the schema maxLength")
	}
}
