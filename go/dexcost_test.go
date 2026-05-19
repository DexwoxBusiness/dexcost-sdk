package dexcost

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

func initLocal(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	Close() // reset any previous init
	err := Init(Config{
		Storage:   "local",
		BufferDir: dir,
	})
	if err != nil {
		t.Fatalf("Init failed: %v", err)
	}
	t.Cleanup(func() { Close() })
}

func TestInit_LocalMode(t *testing.T) {
	dir := t.TempDir()
	Close()
	err := Init(Config{
		Storage:   "local",
		BufferDir: dir,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer Close()

	// Verify database was created.
	dbPath := filepath.Join(dir, "dexcost.db")
	if _, err := os.Stat(dbPath); os.IsNotExist(err) {
		t.Error("expected database file")
	}
}

func TestInit_InvalidKey(t *testing.T) {
	Close()
	err := Init(Config{
		APIKey: "sk-invalid-key",
	})
	if err == nil {
		t.Error("expected error for invalid key")
	}
	Close()
}

func TestStartTask_EndTask_Roundtrip(t *testing.T) {
	initLocal(t)
	ctx, tt := StartTask(context.Background(), "test_roundtrip")
	if tt == nil {
		t.Fatal("expected non-nil TrackedTask")
	}
	if tt.Task.TaskType != "test_roundtrip" {
		t.Errorf("expected test_roundtrip, got %s", tt.Task.TaskType)
	}

	err := tt.End(StatusSuccess)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Also verify context had the task.
	got := core.GetCurrentTask(ctx)
	if got == nil {
		t.Error("expected task in context")
	}
}

func TestRecordCost_OnActiveTask(t *testing.T) {
	initLocal(t)
	ctx, tt := StartTask(context.Background(), "test_record_cost")

	RecordCost(ctx, "stripe", "charge", decimal.NewFromFloat(0.03))

	tt.End(StatusSuccess)

	// Verify aggregated cost.
	stored, err := Tracker().Buffer().GetTask(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	if stored == nil {
		t.Fatal("expected stored task")
	}
}

func TestStartTask_WithOptions(t *testing.T) {
	initLocal(t)
	meta := map[string]interface{}{"env": "test"}
	_, tt := StartTask(context.Background(), "test_opts",
		WithCustomer("acme"),
		WithProject("proj-1"),
		WithMetadata(meta),
	)

	if tt.Task.CustomerID != "acme" {
		t.Errorf("expected customer=acme, got %s", tt.Task.CustomerID)
	}
	if tt.Task.ProjectID != "proj-1" {
		t.Errorf("expected project=proj-1, got %s", tt.Task.ProjectID)
	}
	if tt.Task.Metadata["env"] != "test" {
		t.Error("expected metadata env=test")
	}

	tt.End(StatusSuccess)
}

func TestNestedTasks_ParentLinking(t *testing.T) {
	initLocal(t)
	ctx, parent := StartTask(context.Background(), "parent")
	_, child := StartTask(ctx, "child")

	if child.Task.ParentTaskID == nil {
		t.Fatal("expected parent_task_id to be set")
	}
	if *child.Task.ParentTaskID != parent.Task.TaskID {
		t.Errorf("expected parent=%s, got %s", parent.Task.TaskID, *child.Task.ParentTaskID)
	}

	child.End(StatusSuccess)
	parent.End(StatusSuccess)
}

func TestRecordCost_NoTask(t *testing.T) {
	initLocal(t)
	// Should not panic when no task in context.
	RecordCost(context.Background(), "stripe", "charge", decimal.NewFromFloat(0.01))
}

func TestEndTask_NoTask(t *testing.T) {
	initLocal(t)
	// Should not panic when no task in context.
	EndTask(context.Background(), StatusSuccess)
}

// TestRecordCost_WithOptions verifies that the variadic EventOption arguments
// to the top-level RecordCost (added in DEX-266) round-trip into the buffered
// Event row and override the defaults applied by core.NewEventWithOptions.
func TestRecordCost_WithOptions(t *testing.T) {
	initLocal(t)
	ctx, tt := StartTask(context.Background(), "test_record_cost_options")

	if err := RecordCost(ctx, "openai", "embedding",
		decimal.NewFromFloat(0.0042),
		WithCostConfidence(core.CostConfidenceComputed),
		WithPricingSource(core.PricingSourceLiteLLM),
		WithPricingVersion("2026-05-01"),
		WithDetails(map[string]interface{}{"model": "voyage-3", "tokens": 128}),
	); err != nil {
		t.Fatalf("RecordCost: %v", err)
	}

	// Pull the events for this task back out of the buffer (they are still
	// pending sync, so QueryEvents on the parent task ID returns them).
	events, err := Tracker().Buffer().QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.EventType != core.EventTypeExternalCost {
		t.Errorf("EventType = %q, want %q", ev.EventType, core.EventTypeExternalCost)
	}
	if ev.ServiceName != "openai" {
		t.Errorf("ServiceName = %q, want openai", ev.ServiceName)
	}
	if ev.CostConfidence != core.CostConfidenceComputed {
		t.Errorf("CostConfidence = %q, want computed", ev.CostConfidence)
	}
	if ev.PricingSource != core.PricingSourceLiteLLM {
		t.Errorf("PricingSource = %q, want litellm", ev.PricingSource)
	}
	if ev.PricingVersion != "2026-05-01" {
		t.Errorf("PricingVersion = %q, want 2026-05-01", ev.PricingVersion)
	}
	if got, _ := ev.Details["operation"].(string); got != "embedding" {
		t.Errorf("Details[operation] = %v, want embedding", ev.Details["operation"])
	}
	if got, _ := ev.Details["model"].(string); got != "voyage-3" {
		t.Errorf("Details[model] = %v, want voyage-3", ev.Details["model"])
	}

	tt.End(StatusSuccess)
}

// TestRecordCost_DefaultsWhenNoOptions verifies that without any EventOption,
// RecordCost retains the historical defaults (cost_confidence=exact,
// pricing_source=manual, no pricing_version) so existing callers are unaffected.
func TestRecordCost_DefaultsWhenNoOptions(t *testing.T) {
	initLocal(t)
	ctx, tt := StartTask(context.Background(), "test_record_cost_defaults")

	if err := RecordCost(ctx, "stripe", "charge", decimal.NewFromFloat(0.03)); err != nil {
		t.Fatalf("RecordCost: %v", err)
	}

	events, err := Tracker().Buffer().QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.CostConfidence != core.CostConfidenceExact {
		t.Errorf("CostConfidence = %q, want exact", ev.CostConfidence)
	}
	if ev.PricingSource != core.PricingSourceManual {
		t.Errorf("PricingSource = %q, want manual", ev.PricingSource)
	}
	if ev.PricingVersion != "" {
		t.Errorf("PricingVersion = %q, want empty", ev.PricingVersion)
	}
	if got, _ := ev.Details["operation"].(string); got != "charge" {
		t.Errorf("Details[operation] = %v, want charge", ev.Details["operation"])
	}

	tt.End(StatusSuccess)
}

// TestRecordCost_WithEventTypeComputeCost verifies that the top-level
// RecordCost can emit a compute_cost event when WithEventType is supplied,
// using the top-level EventType constants. The structural fix for the
// hard-coded event type landed in DEX-266; the top-level EventType re-exports
// (so callers don't need to import core) landed in DEX-269. This test exercises
// both via the public API only — no `core` reference for EventType values.
func TestRecordCost_WithEventTypeComputeCost(t *testing.T) {
	initLocal(t)
	ctx, tt := StartTask(context.Background(), "test_record_cost_compute")

	if err := RecordCost(ctx, "lambda", "invoke",
		decimal.NewFromFloat(0.0001),
		WithEventType(EventTypeComputeCost),
	); err != nil {
		t.Fatalf("RecordCost (compute): %v", err)
	}
	if err := RecordCost(ctx, "stripe", "charge",
		decimal.NewFromFloat(0.03),
	); err != nil {
		t.Fatalf("RecordCost (default): %v", err)
	}

	events, err := Tracker().Buffer().QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}

	byService := map[string]Event{}
	for _, ev := range events {
		byService[ev.ServiceName] = ev
	}

	compute, ok := byService["lambda"]
	if !ok {
		t.Fatal("expected an event with ServiceName=lambda")
	}
	if compute.EventType != EventTypeComputeCost {
		t.Errorf("compute EventType = %q, want %q",
			compute.EventType, EventTypeComputeCost)
	}
	if compute.CostUSD.String() != "0.0001" {
		t.Errorf("compute CostUSD = %s, want 0.0001", compute.CostUSD)
	}

	external, ok := byService["stripe"]
	if !ok {
		t.Fatal("expected an event with ServiceName=stripe")
	}
	if external.EventType != EventTypeExternalCost {
		t.Errorf("default EventType = %q, want %q (no override → external_cost)",
			external.EventType, EventTypeExternalCost)
	}

	tt.End(StatusSuccess)
}
