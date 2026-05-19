package tests

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/DexwoxBusiness/dexcost-go/transport"
)

// newTestTracker creates a Tracker backed by a temporary SQLiteBuffer.
func newTestTracker(t *testing.T, dbName string, opts ...func(*core.TrackerOptions)) *core.Tracker {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, dbName)
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("create buffer: %v", err)
	}
	topts := core.TrackerOptions{Buffer: buf}
	for _, o := range opts {
		o(&topts)
	}
	tracker, err := core.NewTracker(topts)
	if err != nil {
		buf.Close()
		t.Fatalf("create tracker: %v", err)
	}
	t.Cleanup(func() { tracker.Close() })
	return tracker
}

// TestEndToEnd_FullWorkflow exercises the complete SDK workflow:
// init -> start task -> record LLM call -> record non-LLM cost -> mark retry -> end task
// Then queries the buffer and verifies all aggregated fields.
func TestEndToEnd_FullWorkflow(t *testing.T) {
	// Create tracker with pricing engine and rate registry.
	rates := pricing.NewRateRegistry()
	rates.Register("google_maps", "request", decimal.RequireFromString("0.005"))

	tracker := newTestTracker(t, "integration.db", func(o *core.TrackerOptions) {
		o.Rates = rates
	})

	// 1. Start a task with customer and project.
	ctx, tt := tracker.StartTask(context.Background(), "resolve_ticket",
		core.WithCustomer("acme-corp"),
		core.WithProject("support-q1"),
	)

	// Verify task was created correctly.
	if tt.Task.TaskType != "resolve_ticket" {
		t.Errorf("expected resolve_ticket, got %s", tt.Task.TaskType)
	}
	if tt.Task.CustomerID != "acme-corp" {
		t.Errorf("expected acme-corp, got %s", tt.Task.CustomerID)
	}
	if tt.Task.ProjectID != "support-q1" {
		t.Errorf("expected support-q1, got %s", tt.Task.ProjectID)
	}
	if tt.Task.Status != core.TaskStatusRunning {
		t.Errorf("expected running, got %s", tt.Task.Status)
	}

	// 2. Record an LLM call (auto-priced from bundled data).
	err := tt.RecordLLMCall("openai", "gpt-4o", 1000, 500)
	if err != nil {
		t.Fatalf("record LLM call: %v", err)
	}

	// 3. Record a non-LLM cost via rate registry.
	err = tt.RecordUsage("google_maps", 3)
	if err != nil {
		t.Fatalf("record usage: %v", err)
	}

	// 4. Record a direct cost.
	err = tt.RecordCost("stripe_api", decimal.RequireFromString("0.025"))
	if err != nil {
		t.Fatalf("record cost: %v", err)
	}

	// 5. Mark a retry.
	retryCost := decimal.RequireFromString("0.003")
	err = tt.MarkRetry("rate_limit", core.WithRetryCost(retryCost))
	if err != nil {
		t.Fatalf("mark retry: %v", err)
	}

	// 6. End the task.
	err = tt.End(core.TaskStatusSuccess)
	if err != nil {
		t.Fatalf("end task: %v", err)
	}

	// 7. Verify aggregated fields.
	stored, err := tracker.Buffer().GetTask(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	if stored == nil {
		t.Fatal("expected stored task")
	}

	// Status should be success.
	if stored.Status != core.TaskStatusSuccess {
		t.Errorf("expected success, got %s", stored.Status)
	}

	// EndedAt should be set.
	if stored.EndedAt == nil {
		t.Error("expected ended_at to be set")
	}

	// LLM cost should be non-zero (auto-priced gpt-4o).
	if stored.LLMCostUSD.IsZero() {
		t.Error("expected non-zero LLM cost (auto-priced gpt-4o)")
	}

	// External cost = google_maps (3 * 0.005 = 0.015) + stripe_api (0.025) = 0.04
	expectedExternal := decimal.RequireFromString("0.04")
	if !stored.ExternalCostUSD.Equal(expectedExternal) {
		t.Errorf("expected external_cost=%s, got %s", expectedExternal, stored.ExternalCostUSD)
	}

	// Total cost = LLM + external + compute.
	expectedTotal := stored.LLMCostUSD.Add(stored.ExternalCostUSD).Add(stored.ComputeCostUSD)
	if !stored.TotalCostUSD.Equal(expectedTotal) {
		t.Errorf("expected total_cost=%s, got %s", expectedTotal, stored.TotalCostUSD)
	}

	// Retry count and cost.
	if stored.RetryCount != 1 {
		t.Errorf("expected retry_count=1, got %d", stored.RetryCount)
	}
	if !stored.RetryCostUSD.Equal(retryCost) {
		t.Errorf("expected retry_cost=%s, got %s", retryCost, stored.RetryCostUSD)
	}

	// Token totals from LLM call.
	if stored.TotalInputTokens != 1000 {
		t.Errorf("expected input_tokens=1000, got %d", stored.TotalInputTokens)
	}
	if stored.TotalOutputTokens != 500 {
		t.Errorf("expected output_tokens=500, got %d", stored.TotalOutputTokens)
	}

	// Failure count should be 0 for success.
	if stored.FailureCount != 0 {
		t.Errorf("expected failure_count=0, got %d", stored.FailureCount)
	}

	// 8. Verify event count and types.
	events, err := tracker.Buffer().QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("query events: %v", err)
	}
	if len(events) != 4 {
		t.Fatalf("expected 4 events, got %d", len(events))
	}

	// Verify event types.
	typeCount := make(map[core.EventType]int)
	for _, e := range events {
		typeCount[e.EventType]++
	}
	if typeCount[core.EventTypeLLMCall] != 1 {
		t.Errorf("expected 1 llm_call, got %d", typeCount[core.EventTypeLLMCall])
	}
	if typeCount[core.EventTypeExternalCost] != 2 {
		t.Errorf("expected 2 external_cost, got %d", typeCount[core.EventTypeExternalCost])
	}
	if typeCount[core.EventTypeRetryMarker] != 1 {
		t.Errorf("expected 1 retry_marker, got %d", typeCount[core.EventTypeRetryMarker])
	}

	// Verify context had the task.
	ctxTask := core.GetCurrentTask(ctx)
	if ctxTask == nil {
		t.Error("expected task in context")
	}
}

// TestEndToEnd_NestedTasks verifies that nested tasks correctly link parent IDs.
func TestEndToEnd_NestedTasks(t *testing.T) {
	tracker := newTestTracker(t, "nested.db")

	// Start parent.
	ctx, parent := tracker.StartTask(context.Background(), "workflow",
		core.WithCustomer("acme"),
	)

	// Start child (should auto-link parent).
	_, child := tracker.StartTask(ctx, "sub_step")

	if child.Task.ParentTaskID == nil {
		t.Fatal("expected parent_task_id on child")
	}
	if *child.Task.ParentTaskID != parent.Task.TaskID {
		t.Errorf("expected parent=%s, got %s", parent.Task.TaskID, *child.Task.ParentTaskID)
	}

	// End both.
	child.End(core.TaskStatusSuccess)
	parent.End(core.TaskStatusSuccess)

	// Verify in buffer.
	storedChild, _ := tracker.Buffer().GetTask(child.Task.TaskID.String())
	if storedChild.ParentTaskID == nil || *storedChild.ParentTaskID != parent.Task.TaskID {
		t.Error("stored child should have parent_task_id")
	}
}

// TestEndToEnd_FailedTask verifies that a failed task has failure_count=1.
func TestEndToEnd_FailedTask(t *testing.T) {
	tracker := newTestTracker(t, "failed.db")

	_, tt := tracker.StartTask(context.Background(), "failing_task")
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, core.WithCost(decimal.NewFromFloat(0.01)))
	tt.End(core.TaskStatusFailed)

	stored, _ := tracker.Buffer().GetTask(tt.Task.TaskID.String())
	if stored.FailureCount != 1 {
		t.Errorf("expected failure_count=1, got %d", stored.FailureCount)
	}
	if stored.Status != core.TaskStatusFailed {
		t.Errorf("expected failed, got %s", stored.Status)
	}
}

// TestEndToEnd_DecimalPrecision verifies that costs maintain full decimal precision
// through the entire pipeline.
func TestEndToEnd_DecimalPrecision(t *testing.T) {
	tracker := newTestTracker(t, "precision.db")

	_, tt := tracker.StartTask(context.Background(), "precision_test")
	preciseCost := decimal.RequireFromString("0.123456789012345678")
	tt.RecordCost("precise_svc", preciseCost)
	tt.End(core.TaskStatusSuccess)

	stored, _ := tracker.Buffer().GetTask(tt.Task.TaskID.String())
	if !stored.ExternalCostUSD.Equal(preciseCost) {
		t.Errorf("precision lost: expected %s, got %s", preciseCost, stored.ExternalCostUSD)
	}
}

// TestEndToEnd_SchemaVersion verifies schema_version is set on tasks and events.
func TestEndToEnd_SchemaVersion(t *testing.T) {
	tracker := newTestTracker(t, "schema.db")

	_, tt := tracker.StartTask(context.Background(), "schema_test")
	tt.RecordCost("svc", decimal.NewFromFloat(0.01))
	tt.End(core.TaskStatusSuccess)

	if tt.Task.SchemaVersion != "1" {
		t.Errorf("expected schema_version=1, got %s", tt.Task.SchemaVersion)
	}

	events, _ := tracker.Buffer().QueryEvents(tt.Task.TaskID.String())
	for _, e := range events {
		if e.SchemaVersion != "1" {
			t.Errorf("expected event schema_version=1, got %s", e.SchemaVersion)
		}
	}
}
