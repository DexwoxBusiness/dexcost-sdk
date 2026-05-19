package core

import (
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

func TestTask_ToDict_RoundTrip(t *testing.T) {
	parentID := uuid.New()
	task := Task{
		TaskID:            uuid.New(),
		TaskType:          "test_task",
		Status:            TaskStatusSuccess,
		StartedAt:         time.Now().UTC().Truncate(time.Millisecond),
		EndedAt:           ptr(time.Now().UTC().Truncate(time.Millisecond)),
		Metadata:          map[string]interface{}{"key": "value"},
		CustomerID:        "acme",
		ProjectID:         "proj-1",
		ParentTaskID:      &parentID,
		ExperimentID:      "exp-1",
		Variant:           "treatment",
		LLMCostUSD:        decimal.RequireFromString("1.23"),
		ExternalCostUSD:   decimal.RequireFromString("0.45"),
		ComputeCostUSD:    decimal.RequireFromString("0.67"),
		TotalCostUSD:      decimal.RequireFromString("2.35"),
		TotalInputTokens:  100,
		TotalOutputTokens: 50,
		TotalCachedTokens: 10,
		RetryCount:        2,
		RetryCostUSD:      decimal.RequireFromString("0.12"),
		FailureCount:      1,
		SchemaVersion:     "1",
	}

	d := task.ToDict()
	restored, err := TaskFromDict(d)
	if err != nil {
		t.Fatalf("TaskFromDict: %v", err)
	}

	if restored.TaskID != task.TaskID {
		t.Errorf("task_id mismatch")
	}
	if restored.TaskType != task.TaskType {
		t.Errorf("task_type mismatch")
	}
	if restored.Status != task.Status {
		t.Errorf("status mismatch")
	}
	if !restored.StartedAt.Equal(task.StartedAt) {
		t.Errorf("started_at mismatch")
	}
	if restored.EndedAt == nil || !restored.EndedAt.Equal(*task.EndedAt) {
		t.Errorf("ended_at mismatch")
	}
	if restored.CustomerID != task.CustomerID {
		t.Errorf("customer_id mismatch")
	}
	if restored.ProjectID != task.ProjectID {
		t.Errorf("project_id mismatch")
	}
	if restored.ParentTaskID == nil || *restored.ParentTaskID != parentID {
		t.Errorf("parent_task_id mismatch")
	}
	if restored.ExperimentID != task.ExperimentID {
		t.Errorf("experiment_id mismatch")
	}
	if restored.Variant != task.Variant {
		t.Errorf("variant mismatch")
	}
	if !restored.LLMCostUSD.Equal(task.LLMCostUSD) {
		t.Errorf("llm_cost_usd mismatch")
	}
	if !restored.ExternalCostUSD.Equal(task.ExternalCostUSD) {
		t.Errorf("external_cost_usd mismatch")
	}
	if !restored.ComputeCostUSD.Equal(task.ComputeCostUSD) {
		t.Errorf("compute_cost_usd mismatch")
	}
	if !restored.TotalCostUSD.Equal(task.TotalCostUSD) {
		t.Errorf("total_cost_usd mismatch")
	}
	if restored.TotalInputTokens != task.TotalInputTokens {
		t.Errorf("total_input_tokens mismatch")
	}
	if restored.TotalOutputTokens != task.TotalOutputTokens {
		t.Errorf("total_output_tokens mismatch")
	}
	if restored.TotalCachedTokens != task.TotalCachedTokens {
		t.Errorf("total_cached_tokens mismatch")
	}
	if restored.RetryCount != task.RetryCount {
		t.Errorf("retry_count mismatch")
	}
	if !restored.RetryCostUSD.Equal(task.RetryCostUSD) {
		t.Errorf("retry_cost_usd mismatch")
	}
	if restored.FailureCount != task.FailureCount {
		t.Errorf("failure_count mismatch")
	}
	if restored.SchemaVersion != task.SchemaVersion {
		t.Errorf("schema_version mismatch")
	}
}

func TestTask_FromDict_Minimal(t *testing.T) {
	d := map[string]interface{}{
		"task_id":   uuid.New().String(),
		"task_type": "minimal",
		"status":    "pending",
	}
	task, err := TaskFromDict(d)
	if err != nil {
		t.Fatalf("TaskFromDict: %v", err)
	}
	if task.TaskType != "minimal" {
		t.Errorf("expected minimal, got %s", task.TaskType)
	}
	if task.Status != TaskStatusPending {
		t.Errorf("expected pending, got %s", task.Status)
	}
}

func TestTask_FromDict_RunningStatus(t *testing.T) {
	d := map[string]interface{}{
		"task_id":   uuid.New().String(),
		"task_type": "running_task",
		"status":    "running",
	}
	task, err := TaskFromDict(d)
	if err != nil {
		t.Fatalf("TaskFromDict: %v", err)
	}
	if task.Status != TaskStatusRunning {
		t.Errorf("expected running, got %s", task.Status)
	}
}

func TestEvent_ToDict_RoundTrip(t *testing.T) {
	retryOf := uuid.New()
	event := Event{
		EventID:        uuid.New(),
		TaskID:         uuid.New(),
		EventType:      EventTypeLLMCall,
		OccurredAt:     time.Now().UTC().Truncate(time.Millisecond),
		CostUSD:        decimal.RequireFromString("0.0123"),
		CostConfidence: CostConfidenceComputed,
		PricingSource:  PricingSourceLiteLLM,
		PricingVersion: "v1.2.3",
		ServiceName:    "openai",
		Provider:       "openai",
		Model:          "gpt-4o",
		ErrorType:      "rate_limit",
		InputTokens:    ptr(1000),
		OutputTokens:   ptr(500),
		CachedTokens:   ptr(100),
		LatencyMs:      ptr(250),
		IsRetry:        true,
		RetryReason:    "timeout",
		RetryOf:        &retryOf,
		Details:        map[string]interface{}{"batch": true},
		SchemaVersion:  "1",
	}

	d := event.ToDict()
	restored, err := EventFromDict(d)
	if err != nil {
		t.Fatalf("EventFromDict: %v", err)
	}

	if restored.EventID != event.EventID {
		t.Errorf("event_id mismatch")
	}
	if restored.TaskID != event.TaskID {
		t.Errorf("task_id mismatch")
	}
	if restored.EventType != event.EventType {
		t.Errorf("event_type mismatch")
	}
	if !restored.OccurredAt.Equal(event.OccurredAt) {
		t.Errorf("occurred_at mismatch")
	}
	if !restored.CostUSD.Equal(event.CostUSD) {
		t.Errorf("cost_usd mismatch")
	}
	if restored.CostConfidence != event.CostConfidence {
		t.Errorf("cost_confidence mismatch")
	}
	if restored.PricingSource != event.PricingSource {
		t.Errorf("pricing_source mismatch")
	}
	if restored.PricingVersion != event.PricingVersion {
		t.Errorf("pricing_version mismatch")
	}
	if restored.ServiceName != event.ServiceName {
		t.Errorf("service_name mismatch")
	}
	if restored.Provider != event.Provider {
		t.Errorf("provider mismatch")
	}
	if restored.Model != event.Model {
		t.Errorf("model mismatch")
	}
	if restored.ErrorType != event.ErrorType {
		t.Errorf("error_type mismatch")
	}
	if restored.InputTokens == nil || *restored.InputTokens != 1000 {
		t.Errorf("input_tokens mismatch")
	}
	if restored.OutputTokens == nil || *restored.OutputTokens != 500 {
		t.Errorf("output_tokens mismatch")
	}
	if restored.CachedTokens == nil || *restored.CachedTokens != 100 {
		t.Errorf("cached_tokens mismatch")
	}
	if restored.LatencyMs == nil || *restored.LatencyMs != 250 {
		t.Errorf("latency_ms mismatch")
	}
	if restored.IsRetry != event.IsRetry {
		t.Errorf("is_retry mismatch")
	}
	if restored.RetryReason != event.RetryReason {
		t.Errorf("retry_reason mismatch")
	}
	if restored.RetryOf == nil || *restored.RetryOf != retryOf {
		t.Errorf("retry_of mismatch")
	}
	if restored.SchemaVersion != event.SchemaVersion {
		t.Errorf("schema_version mismatch")
	}
}

func TestEvent_FromDict_Minimal(t *testing.T) {
	d := map[string]interface{}{
		"event_id":   uuid.New().String(),
		"task_id":    uuid.New().String(),
		"event_type": "external_cost",
	}
	event, err := EventFromDict(d)
	if err != nil {
		t.Fatalf("EventFromDict: %v", err)
	}
	if event.EventType != EventTypeExternalCost {
		t.Errorf("expected external_cost, got %s", event.EventType)
	}
}

func ptr[T any](v T) *T {
	return &v
}
