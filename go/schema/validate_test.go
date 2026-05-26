package schema

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestValidate_CorrectEvent(t *testing.T) {
	payload := map[string]interface{}{
		"event_id":        "550e8400-e29b-41d4-a716-446655440000",
		"task_id":         "550e8400-e29b-41d4-a716-446655440001",
		"event_type":      "llm_call",
		"occurred_at":     "2026-04-04T10:00:00Z",
		"cost_usd":        "0.05",
		"cost_confidence": "computed",
		"is_retry":        false,
		"details":         map[string]interface{}{},
		"schema_version":  "1",
	}
	errs := Validate(payload)
	if errs != nil {
		t.Errorf("expected nil errors, got %v", errs)
	}
}

func TestValidate_CorrectTask(t *testing.T) {
	payload := map[string]interface{}{
		"task_id":             "550e8400-e29b-41d4-a716-446655440000",
		"task_type":           "summarize",
		"status":              "success",
		"started_at":          "2026-04-04T10:00:00Z",
		"schema_version":      "1",
		"llm_cost_usd":        "0.05",
		"external_cost_usd":   "0.01",
		"compute_cost_usd":    "0.00",
		"total_cost_usd":      "0.06",
		"total_input_tokens":  100,
		"total_output_tokens": 50,
		"total_cached_tokens": 0,
		"retry_count":         0,
		"retry_cost_usd":      "0",
		"failure_count":       0,
	}
	errs := Validate(payload)
	if errs != nil {
		t.Errorf("expected nil errors, got %v", errs)
	}
}

func TestValidate_InvalidEvent(t *testing.T) {
	payload := map[string]interface{}{
		"event_id":       "550e8400-e29b-41d4-a716-446655440000",
		"schema_version": "1",
	}
	errs := Validate(payload)
	if len(errs) == 0 {
		t.Error("expected errors for invalid event")
	}
}

func TestValidate_InvalidTask(t *testing.T) {
	payload := map[string]interface{}{
		"task_id":        "550e8400-e29b-41d4-a716-446655440000",
		"schema_version": "1",
	}
	errs := Validate(payload)
	if len(errs) == 0 {
		t.Error("expected errors for invalid task")
	}
}

// B6 regression — Sprint 1 Theme F / plan §2.3.2.
//
// The Go event schema's event_type enum at dexcost-event.v1.json:30 was
// missing "gpu_cost" and "gpu_utilization_signal" — the canonical
// Sprint 0 fixtures for both event types failed Validate(), even
// though Python/TS schemas already include them. Symmetric fix: add
// the two values to the Go enum.
func TestValidate_GpuCostFixtureAccepted(t *testing.T) {
	for _, fixture := range []string{
		"../../fixtures/events/gpu_cost.v1.json",
		"../../fixtures/events/gpu_utilization_signal.v1.json",
	} {
		t.Run(filepath.Base(fixture), func(t *testing.T) {
			raw, err := os.ReadFile(fixture)
			if err != nil {
				t.Fatalf("read %s: %v", fixture, err)
			}
			var payload map[string]interface{}
			if err := json.Unmarshal(raw, &payload); err != nil {
				t.Fatalf("unmarshal %s: %v", fixture, err)
			}
			// Drop underscored audit-only keys (matches the cross-SDK
			// consumer's stripUnderscoredKeys behaviour).
			for k := range payload {
				if len(k) > 0 && k[0] == '_' {
					delete(payload, k)
				}
			}
			if errs := Validate(payload); errs != nil {
				t.Errorf("expected %s to validate, got errors: %v", fixture, errs)
			}
		})
	}
}

// B6 — also assert the Task schema accepts network_cost_usd and
// gpu_cost_usd fields. Pre-fix the Go task schema didn't declare them,
// so payloads carrying these subsystem totals would fail validation.
func TestValidate_TaskWithNetworkAndGpuCostFields(t *testing.T) {
	payload := map[string]interface{}{
		"task_id":             "550e8400-e29b-41d4-a716-446655440000",
		"task_type":           "summarize",
		"status":              "success",
		"started_at":          "2026-04-04T10:00:00Z",
		"schema_version":      "1",
		"llm_cost_usd":        "0.05",
		"external_cost_usd":   "0.00",
		"compute_cost_usd":    "0.00",
		"network_cost_usd":    "0.01",
		"gpu_cost_usd":        "0.02",
		"total_cost_usd":      "0.08",
		"total_input_tokens":  100,
		"total_output_tokens": 50,
		"total_cached_tokens": 0,
		"retry_count":         0,
		"retry_cost_usd":      "0.00",
		"failure_count":       0,
		"metadata":            map[string]interface{}{},
	}
	if errs := Validate(payload); errs != nil {
		t.Errorf("expected task with network_cost_usd / gpu_cost_usd to validate, got %v", errs)
	}
}

func TestValidate_UnsupportedVersion(t *testing.T) {
	payload := map[string]interface{}{"schema_version": "99"}
	errs := Validate(payload)
	if len(errs) != 1 || errs[0] != "Unsupported schema_version: 99" {
		t.Errorf("expected version error, got %v", errs)
	}
}

func TestValidate_NeitherTaskNorEvent(t *testing.T) {
	payload := map[string]interface{}{"schema_version": "1"}
	errs := Validate(payload)
	if len(errs) != 1 || errs[0] != "Cannot determine payload type: missing task_id or event_id" {
		t.Errorf("expected type error, got %v", errs)
	}
}
