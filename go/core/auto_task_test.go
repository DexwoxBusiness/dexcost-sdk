package core

import (
	"context"
	"testing"

	"github.com/shopspring/decimal"
)

func TestNeedsAutoTask_True(t *testing.T) {
	ctx := context.Background()
	if !NeedsAutoTask(ctx) {
		t.Error("expected true when no task in context")
	}
}

func TestNeedsAutoTask_False(t *testing.T) {
	task := NewTask("test")
	ctx := WithTask(context.Background(), &task)
	if NeedsAutoTask(ctx) {
		t.Error("expected false when task is in context")
	}
}

func TestCreateAutoTask_WithContext(t *testing.T) {
	ctx := context.Background()
	ctx = SetContext(ctx, &ContextData{
		CustomerID: "auto-customer",
		ProjectID:  "auto-project",
	})

	task := CreateAutoTask(ctx, "openai.chat")
	if task.CustomerID != "auto-customer" {
		t.Errorf("expected auto-customer, got %s", task.CustomerID)
	}
	if task.ProjectID != "auto-project" {
		t.Errorf("expected auto-project, got %s", task.ProjectID)
	}
	if task.TaskType != "openai.chat" {
		t.Errorf("expected openai.chat, got %s", task.TaskType)
	}
}

func TestCreateAutoTask_WithoutContext(t *testing.T) {
	ctx := context.Background()
	task := CreateAutoTask(ctx, "test.call")
	if task.CustomerID != "" {
		t.Error("expected empty customer_id without context")
	}
}

func TestFinalizeAutoTask_NilTask(t *testing.T) {
	// Should not panic.
	FinalizeAutoTask(nil, nil, "success", nil)
}

func TestFinalizeAutoTask_NilEvent(t *testing.T) {
	task := NewTask("auto_test")
	FinalizeAutoTask(&task, nil, "success", nil)

	if task.Status != TaskStatusSuccess {
		t.Errorf("expected status=success, got %s", task.Status)
	}
	if task.EndedAt == nil {
		t.Error("expected ended_at to be set")
	}
	if !task.TotalCostUSD.IsZero() {
		t.Errorf("expected zero total cost, got %s", task.TotalCostUSD)
	}
}

func TestFinalizeAutoTask_LLMCallEvent(t *testing.T) {
	task := NewTask("auto_llm")
	inTok := 100
	outTok := 50
	cached := 20
	event := Event{
		EventType:    EventTypeLLMCall,
		CostUSD:      decimal.NewFromFloat(0.05),
		InputTokens:  &inTok,
		OutputTokens: &outTok,
		CachedTokens: &cached,
	}

	FinalizeAutoTask(&task, &event, "success", nil)

	if !task.LLMCostUSD.Equal(decimal.NewFromFloat(0.05)) {
		t.Errorf("expected llm_cost_usd=0.05, got %s", task.LLMCostUSD)
	}
	if !task.TotalCostUSD.Equal(decimal.NewFromFloat(0.05)) {
		t.Errorf("expected total_cost_usd=0.05, got %s", task.TotalCostUSD)
	}
	if task.TotalInputTokens != 100 {
		t.Errorf("expected total_input_tokens=100, got %d", task.TotalInputTokens)
	}
	if task.TotalOutputTokens != 50 {
		t.Errorf("expected total_output_tokens=50, got %d", task.TotalOutputTokens)
	}
	if task.TotalCachedTokens != 20 {
		t.Errorf("expected total_cached_tokens=20, got %d", task.TotalCachedTokens)
	}
}

func TestFinalizeAutoTask_ExternalCostEvent(t *testing.T) {
	task := NewTask("auto_ext")
	event := Event{
		EventType: EventTypeExternalCost,
		CostUSD:   decimal.NewFromFloat(0.01),
	}

	FinalizeAutoTask(&task, &event, "success", nil)

	if !task.ExternalCostUSD.Equal(decimal.NewFromFloat(0.01)) {
		t.Errorf("expected external_cost_usd=0.01, got %s", task.ExternalCostUSD)
	}
	if !task.TotalCostUSD.Equal(decimal.NewFromFloat(0.01)) {
		t.Errorf("expected total_cost_usd=0.01, got %s", task.TotalCostUSD)
	}
}

func TestFinalizeAutoTask_ComputeCostEvent(t *testing.T) {
	task := NewTask("auto_compute")
	event := Event{
		EventType: EventTypeComputeCost,
		CostUSD:   decimal.NewFromFloat(0.02),
	}

	FinalizeAutoTask(&task, &event, "success", nil)

	if !task.ComputeCostUSD.Equal(decimal.NewFromFloat(0.02)) {
		t.Errorf("expected compute_cost_usd=0.02, got %s", task.ComputeCostUSD)
	}
	if !task.TotalCostUSD.Equal(decimal.NewFromFloat(0.02)) {
		t.Errorf("expected total_cost_usd=0.02, got %s", task.TotalCostUSD)
	}
}

func TestFinalizeAutoTask_RetryEvent(t *testing.T) {
	task := NewTask("auto_retry")
	event := Event{
		EventType: EventTypeLLMCall,
		CostUSD:   decimal.NewFromFloat(0.03),
		IsRetry:   true,
	}

	FinalizeAutoTask(&task, &event, "success", nil)

	if task.RetryCount != 1 {
		t.Errorf("expected retry_count=1, got %d", task.RetryCount)
	}
	if !task.RetryCostUSD.Equal(decimal.NewFromFloat(0.03)) {
		t.Errorf("expected retry_cost_usd=0.03, got %s", task.RetryCostUSD)
	}
}

func TestFinalizeAutoTask_FailedStatus(t *testing.T) {
	task := NewTask("auto_fail")
	FinalizeAutoTask(&task, nil, "failed", nil)

	if task.Status != TaskStatusFailed {
		t.Errorf("expected status=failed, got %s", task.Status)
	}
	if task.FailureCount != 1 {
		t.Errorf("expected failure_count=1, got %d", task.FailureCount)
	}
}
