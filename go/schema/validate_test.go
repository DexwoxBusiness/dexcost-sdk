package schema

import "testing"

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
