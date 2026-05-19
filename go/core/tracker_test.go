package core

import (
	"context"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// mockBuffer is an in-memory Buffer implementation for unit tests.
// It avoids any SQLite/transport dependency in the core package tests.
type mockBuffer struct {
	mu     sync.Mutex
	tasks  map[string]Task
	events []Event
}

func newMockBuffer() *mockBuffer {
	return &mockBuffer{
		tasks:  make(map[string]Task),
		events: []Event{},
	}
}

func (m *mockBuffer) InsertTask(task Task) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.tasks[task.TaskID.String()] = task
	return nil
}

func (m *mockBuffer) UpdateTask(task Task) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.tasks[task.TaskID.String()] = task
	return nil
}

func (m *mockBuffer) GetTask(taskID string) (*Task, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	t, ok := m.tasks[taskID]
	if !ok {
		return nil, nil
	}
	return &t, nil
}

func (m *mockBuffer) InsertEvent(event Event) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events = append(m.events, event)
	return nil
}

func (m *mockBuffer) UpdateEvent(event Event) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, e := range m.events {
		if e.EventID == event.EventID {
			m.events[i] = event
			return nil
		}
	}
	return nil
}

func (m *mockBuffer) QueryEvents(taskID string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	tid, err := uuid.Parse(taskID)
	if err != nil {
		return nil, err
	}
	var out []Event
	for _, e := range m.events {
		if e.TaskID == tid {
			out = append(out, e)
		}
	}
	return out, nil
}

func (m *mockBuffer) QueryPendingEvents(limit int) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []Event
	for _, e := range m.events {
		if len(out) >= limit {
			break
		}
		out = append(out, e)
	}
	return out, nil
}

func (m *mockBuffer) MarkSynced(eventIDs []string) error {
	return nil
}

func (m *mockBuffer) Close() error {
	return nil
}

func newTestTracker(t *testing.T) *Tracker {
	t.Helper()
	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("failed to create tracker: %v", err)
	}
	t.Cleanup(func() { tr.Close() })
	return tr
}

func TestTracker_StartTask_ValidUUID(t *testing.T) {
	tr := newTestTracker(t)
	ctx, tt := tr.StartTask(context.Background(), "resolve_ticket")
	if tt.Task.TaskID == uuid.Nil {
		t.Error("expected non-nil task_id")
	}
	if tt.Task.TaskType != "resolve_ticket" {
		t.Errorf("expected resolve_ticket, got %s", tt.Task.TaskType)
	}
	// Verify context has task
	got := GetCurrentTask(ctx)
	if got == nil || got.TaskID != tt.Task.TaskID {
		t.Error("expected task in context")
	}
}

func TestTracker_StartTask_TransitionsToRunning(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "long_running_job")
	if tt.Task.Status != TaskStatusRunning {
		t.Errorf("StartTask should leave task in running status; got %s", tt.Task.Status)
	}
	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}
	if tt.Task.Status != TaskStatusSuccess {
		t.Errorf("after End(Success), expected success, got %s", tt.Task.Status)
	}
}

func TestTracker_EndTask_Success(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_end")
	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if tt.Task.Status != TaskStatusSuccess {
		t.Errorf("expected success, got %s", tt.Task.Status)
	}
	if tt.Task.EndedAt == nil {
		t.Error("expected ended_at to be set")
	}
}

func TestTracker_EndTask_Failed(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_fail")
	tt.End(TaskStatusFailed)
	if tt.Task.Status != TaskStatusFailed {
		t.Errorf("expected failed, got %s", tt.Task.Status)
	}
	if tt.Task.FailureCount != 1 {
		t.Errorf("expected failure_count=1, got %d", tt.Task.FailureCount)
	}
}

func TestTracker_EndTask_Twice(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_double_end")
	tt.End(TaskStatusSuccess)
	err := tt.End(TaskStatusFailed)
	if err != ErrTaskAlreadyEnded {
		t.Errorf("expected ErrTaskAlreadyEnded, got %v", err)
	}
}

func TestTracker_RecordCost(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_cost")
	err := tt.RecordCost("google_maps", decimal.NewFromFloat(0.005))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	if !tt.Task.ExternalCostUSD.Equal(decimal.NewFromFloat(0.005)) {
		t.Errorf("expected external_cost=0.005, got %s", tt.Task.ExternalCostUSD)
	}
	if !tt.Task.TotalCostUSD.Equal(decimal.NewFromFloat(0.005)) {
		t.Errorf("expected total_cost=0.005, got %s", tt.Task.TotalCostUSD)
	}
}

func TestTracker_RecordLLMCall_ManualCost(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_llm")
	cost := decimal.NewFromFloat(0.01)
	err := tt.RecordLLMCall("openai", "gpt-4o", 500, 200, WithCost(cost))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	if !tt.Task.LLMCostUSD.Equal(decimal.NewFromFloat(0.01)) {
		t.Errorf("expected llm_cost=0.01, got %s", tt.Task.LLMCostUSD)
	}
	if tt.Task.TotalInputTokens != 500 {
		t.Errorf("expected input_tokens=500, got %d", tt.Task.TotalInputTokens)
	}
	if tt.Task.TotalOutputTokens != 200 {
		t.Errorf("expected output_tokens=200, got %d", tt.Task.TotalOutputTokens)
	}
}

func TestTracker_RecordLLMCall_AutoPrice(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_auto_price")
	// No WithCost -- should auto-compute from pricing engine
	err := tt.RecordLLMCall("openai", "gpt-4o", 1000, 500)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	// The auto-computed cost depends on the bundled pricing data.
	// Just verify it's not zero (gpt-4o should be in the bundled data).
	if tt.Task.LLMCostUSD.IsZero() {
		t.Error("expected non-zero auto-priced LLM cost")
	}
	if tt.Task.TotalCostUSD.IsZero() {
		t.Error("expected non-zero total cost")
	}
}

func TestTracker_MarkRetry(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_retry")
	retryCost := decimal.NewFromFloat(0.02)
	err := tt.MarkRetry("rate_limit", WithRetryCost(retryCost))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	if tt.Task.RetryCount != 1 {
		t.Errorf("expected retry_count=1, got %d", tt.Task.RetryCount)
	}
	if !tt.Task.RetryCostUSD.Equal(decimal.NewFromFloat(0.02)) {
		t.Errorf("expected retry_cost=0.02, got %s", tt.Task.RetryCostUSD)
	}
}

func TestTracker_CostAggregation(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_agg")

	llmCost := decimal.NewFromFloat(0.05)
	tt.RecordLLMCall("openai", "gpt-4o", 1000, 500, WithCost(llmCost))
	tt.RecordCost("google_maps", decimal.NewFromFloat(0.01))
	tt.MarkRetry("timeout", WithRetryCost(decimal.NewFromFloat(0.005)))

	tt.End(TaskStatusSuccess)

	if !tt.Task.LLMCostUSD.Equal(decimal.NewFromFloat(0.05)) {
		t.Errorf("expected llm_cost=0.05, got %s", tt.Task.LLMCostUSD)
	}
	if !tt.Task.ExternalCostUSD.Equal(decimal.NewFromFloat(0.01)) {
		t.Errorf("expected external_cost=0.01, got %s", tt.Task.ExternalCostUSD)
	}
	expectedTotal := decimal.NewFromFloat(0.06)
	if !tt.Task.TotalCostUSD.Equal(expectedTotal) {
		t.Errorf("expected total_cost=%s, got %s", expectedTotal, tt.Task.TotalCostUSD)
	}
	if tt.Task.RetryCount != 1 {
		t.Errorf("expected retry_count=1, got %d", tt.Task.RetryCount)
	}
}

func TestTracker_NestedTasks_ParentLinking(t *testing.T) {
	tr := newTestTracker(t)
	ctx, parent := tr.StartTask(context.Background(), "parent_task")
	_, child := tr.StartTask(ctx, "child_task")

	if child.Task.ParentTaskID == nil {
		t.Fatal("expected parent_task_id to be set")
	}
	if *child.Task.ParentTaskID != parent.Task.TaskID {
		t.Errorf("expected parent_task_id=%s, got %s", parent.Task.TaskID, *child.Task.ParentTaskID)
	}

	child.End(TaskStatusSuccess)
	parent.End(TaskStatusSuccess)
}

func TestTracker_WithOptions(t *testing.T) {
	tr := newTestTracker(t)
	meta := map[string]interface{}{"env": "production"}
	_, tt := tr.StartTask(context.Background(), "test_opts",
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
	if tt.Task.Metadata["env"] != "production" {
		t.Errorf("expected metadata env=production")
	}

	tt.End(TaskStatusSuccess)
}

func TestTracker_RecordUsage_WithRate(t *testing.T) {
	tr := newTestTracker(t)
	tr.Rates().Register("google_maps", "request", decimal.RequireFromString("0.005"))

	_, tt := tr.StartTask(context.Background(), "test_usage")
	err := tt.RecordUsage("google_maps", 10)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	expected := decimal.RequireFromString("0.05")
	if !tt.Task.ExternalCostUSD.Equal(expected) {
		t.Errorf("expected external_cost=%s, got %s", expected, tt.Task.ExternalCostUSD)
	}
}

func TestTracker_RecordUsage_UnknownService(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_usage_unknown")
	err := tt.RecordUsage("unknown_service", 5)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	tt.End(TaskStatusSuccess)

	if !tt.Task.ExternalCostUSD.IsZero() {
		t.Errorf("expected zero cost for unknown service, got %s", tt.Task.ExternalCostUSD)
	}
}

func TestTracker_ContextCleanup(t *testing.T) {
	tr := newTestTracker(t)
	ctx, tt := tr.StartTask(context.Background(), "test_ctx")

	// During task, context should have task.
	got := GetCurrentTask(ctx)
	if got == nil {
		t.Fatal("expected task in context")
	}

	tt.End(TaskStatusSuccess)

	// After end, the original context still has the reference
	// (Go contexts are immutable), but the task should be ended.
	got2 := GetCurrentTask(ctx)
	if got2 == nil {
		t.Fatal("context still holds task reference")
	}
	if got2.Status != TaskStatusSuccess {
		t.Errorf("expected success status on context task")
	}
}

// TestTracker_PricingEngine_Integration verifies that the pricing engine
// is accessible and functional from the tracker.
func TestTracker_PricingEngine_Integration(t *testing.T) {
	tr := newTestTracker(t)
	eng := tr.Pricing()
	if eng == nil {
		t.Fatal("expected non-nil pricing engine")
	}
	result := eng.GetCost("gpt-4o", 1000, 500, 0, 0)
	if result.CostUSD.IsZero() {
		t.Error("expected non-zero cost from bundled pricing data")
	}
}

// TestTracker_RateRegistry_Integration verifies the rate registry integration.
func TestTracker_RateRegistry_Integration(t *testing.T) {
	tr := newTestTracker(t)
	tr.Rates().Register("stripe_api", "request", decimal.RequireFromString("0.0035"))
	entry := tr.Rates().Get("stripe_api")
	if entry == nil {
		t.Fatal("expected rate entry")
	}
	if !entry.CostUSD.Equal(decimal.RequireFromString("0.0035")) {
		t.Errorf("expected 0.0035, got %s", entry.CostUSD)
	}
}

func TestTracker_WithExperiment(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_experiment",
		WithCustomer("acme"),
		WithExperiment("exp-pricing-v2"),
		WithVariant("treatment"),
	)

	if tt.Task.ExperimentID != "exp-pricing-v2" {
		t.Errorf("expected experiment_id=exp-pricing-v2, got %s", tt.Task.ExperimentID)
	}
	if tt.Task.Variant != "treatment" {
		t.Errorf("expected variant=treatment, got %s", tt.Task.Variant)
	}

	tt.End(TaskStatusSuccess)

	// Verify persisted via buffer
	got, err := tr.Buffer().GetTask(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.ExperimentID != "exp-pricing-v2" {
		t.Errorf("persisted experiment_id=%s, want exp-pricing-v2", got.ExperimentID)
	}
	if got.Variant != "treatment" {
		t.Errorf("persisted variant=%s, want treatment", got.Variant)
	}
}

func TestTracker_ExperimentFieldsOptional(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "no_experiment")
	tt.End(TaskStatusSuccess)

	got, err := tr.Buffer().GetTask(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.ExperimentID != "" {
		t.Errorf("expected empty experiment_id, got %s", got.ExperimentID)
	}
	if got.Variant != "" {
		t.Errorf("expected empty variant, got %s", got.Variant)
	}
}

// ---- MarkNotRetry tests ----

func TestTrackedTask_MarkNotRetry_MostRecent(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_mark_not_retry")

	// Record an LLM call then mark it as a retry.
	cost := decimal.NewFromFloat(0.01)
	err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Manually mark the event as retry via UpdateEvent.
	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	events[0].IsRetry = true
	events[0].RetryReason = "rate_limit"
	tr.Buffer().UpdateEvent(events[0])

	// Verify it's flagged as retry.
	events, _ = tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if !events[0].IsRetry {
		t.Fatal("expected event to be flagged as retry")
	}

	// Now un-mark with uuid.Nil (most-recent / first found).
	err = tt.MarkNotRetry(uuid.Nil)
	if err != nil {
		t.Fatalf("MarkNotRetry returned error: %v", err)
	}

	events, _ = tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if events[0].IsRetry {
		t.Error("expected IsRetry=false after MarkNotRetry")
	}
	if events[0].RetryReason != "" {
		t.Errorf("expected empty RetryReason, got %q", events[0].RetryReason)
	}
	if events[0].RetryOf != nil {
		t.Error("expected RetryOf=nil after MarkNotRetry")
	}
}

func TestTrackedTask_MarkNotRetry_ByID(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_mark_not_retry_byid")

	cost := decimal.NewFromFloat(0.01)

	// Record two LLM calls.
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))

	// Mark both as retry.
	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	firstID := events[0].EventID
	for _, e := range events {
		e.IsRetry = true
		e.RetryReason = "timeout"
		tr.Buffer().UpdateEvent(e)
	}

	// Un-mark only the first event by ID.
	err := tt.MarkNotRetry(firstID)
	if err != nil {
		t.Fatalf("MarkNotRetry returned error: %v", err)
	}

	events, _ = tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	for _, e := range events {
		if e.EventID == firstID {
			if e.IsRetry {
				t.Error("expected first event IsRetry=false")
			}
		} else {
			if !e.IsRetry {
				t.Error("expected second event IsRetry=true (unchanged)")
			}
		}
	}
}

func TestTrackedTask_MarkNotRetry_NoRetries(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_mark_not_retry_none")

	cost := decimal.NewFromFloat(0.01)
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))

	// No retries present — should be a no-op.
	err := tt.MarkNotRetry(uuid.Nil)
	if err != nil {
		t.Fatalf("expected no-op, got error: %v", err)
	}

	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].IsRetry {
		t.Error("expected IsRetry=false")
	}
}

// ---- Heuristic wiring tests ----

func newTestTrackerWithHeuristics(t *testing.T, windowSeconds, threshold float64) *Tracker {
	t.Helper()
	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{
		Buffer:                  buf,
		EnableRetryHeuristics:   true,
		RetryHeuristicWindow:    windowSeconds,
		RetryHeuristicThreshold: threshold,
	})
	if err != nil {
		t.Fatalf("failed to create tracker: %v", err)
	}
	t.Cleanup(func() { tr.Close() })
	return tr
}

func TestTrackedTask_RecordLLMCall_HeuristicDetection(t *testing.T) {
	// Use a very low threshold so the heuristic triggers easily.
	tr := newTestTrackerWithHeuristics(t, 30, 0.1)
	_, tt := tr.StartTask(context.Background(), "test_heuristic_detect")

	cost := decimal.NewFromFloat(0.01)

	// Record first call and insert it into the heuristic engine manually
	// (simulating a failed call with error_type).
	err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))
	if err != nil {
		t.Fatalf("first RecordLLMCall error: %v", err)
	}

	// Retrieve the first event and set error_type, then update buffer
	// and manually record it in the heuristic engine.
	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	firstEvent := events[0]
	firstEvent.Details["error_type"] = "rate_limit"
	tr.Buffer().UpdateEvent(firstEvent)

	// Manually record into heuristics window (the first call was inserted
	// before error_type was set, so we update the engine directly).
	tr.heuristics.Record(firstEvent)

	// Record a second call — heuristic should flag it as a retry.
	err = tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))
	if err != nil {
		t.Fatalf("second RecordLLMCall error: %v", err)
	}

	events, _ = tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}

	// The second event should be flagged as retry.
	secondEvent := events[1]
	if !secondEvent.IsRetry {
		t.Error("expected second event to be flagged as retry by heuristic")
	}
	if secondEvent.RetryReason != "heuristic" {
		t.Errorf("expected RetryReason=heuristic, got %q", secondEvent.RetryReason)
	}
	if secondEvent.RetryOf == nil {
		t.Error("expected RetryOf to be set to first event ID")
	} else if *secondEvent.RetryOf != firstEvent.EventID {
		t.Errorf("expected RetryOf=%s, got %s", firstEvent.EventID, *secondEvent.RetryOf)
	}
}

func TestTrackedTask_RecordLLMCall_NoHeuristics(t *testing.T) {
	// Heuristics disabled — two calls, neither should be flagged.
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_no_heuristics")

	cost := decimal.NewFromFloat(0.01)
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))
	tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithCost(cost))

	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	for i, e := range events {
		if e.IsRetry {
			t.Errorf("event[%d] should not be flagged as retry (heuristics disabled)", i)
		}
	}
}

func TestTracker_RecordCost_WithOptions(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_cost_opts")

	err := tt.RecordCost("stripe_api", decimal.RequireFromString("0.025"),
		WithOperation("payment_lookup"),
		WithEventType(EventTypeComputeCost),
		WithDetails(map[string]interface{}{"currency": "usd"}),
		WithCostConfidence(CostConfidenceEstimated),
		WithPricingSource(PricingSourceCustom),
		WithPricingVersion("v2"),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.EventType != EventTypeComputeCost {
		t.Errorf("expected event_type=compute_cost, got %s", ev.EventType)
	}
	if ev.Details["operation"] != "payment_lookup" {
		t.Errorf("expected operation=payment_lookup, got %v", ev.Details["operation"])
	}
	if ev.Details["currency"] != "usd" {
		t.Errorf("expected currency=usd, got %v", ev.Details["currency"])
	}
	if ev.CostConfidence != CostConfidenceEstimated {
		t.Errorf("expected cost_confidence=estimated, got %s", ev.CostConfidence)
	}
	if ev.PricingSource != PricingSourceCustom {
		t.Errorf("expected pricing_source=custom, got %s", ev.PricingSource)
	}
	if ev.PricingVersion != "v2" {
		t.Errorf("expected pricing_version=v2, got %s", ev.PricingVersion)
	}
}

func TestTracker_RecordLLMCall_WithErrorType(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_llm_err")

	err := tt.RecordLLMCall("openai", "gpt-4o", 1000, 500,
		WithCachedTokens(100),
		WithLatency(250),
		WithErrorType("rate_limit"),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.CachedTokens == nil || *ev.CachedTokens != 100 {
		t.Errorf("expected cached_tokens=100, got %v", ev.CachedTokens)
	}
	if ev.LatencyMs == nil || *ev.LatencyMs != 250 {
		t.Errorf("expected latency_ms=250, got %v", ev.LatencyMs)
	}
	if ev.ErrorType != "rate_limit" {
		t.Errorf("expected error_type=rate_limit, got %s", ev.ErrorType)
	}
}

func TestTrackedTask_LinkTrace(t *testing.T) {
	tr := newTestTracker(t)
	_, tt := tr.StartTask(context.Background(), "test_trace")

	tt.LinkTrace("langsmith", "trace-123")
	tt.LinkTrace("datadog", "trace-456")

	links := tt.GetTraceLinks()
	if len(links) != 2 {
		t.Fatalf("expected 2 trace links, got %d", len(links))
	}
	if links[0]["provider"] != "langsmith" || links[0]["trace_id"] != "trace-123" {
		t.Errorf("first link mismatch: %v", links[0])
	}
	if links[1]["provider"] != "datadog" || links[1]["trace_id"] != "trace-456" {
		t.Errorf("second link mismatch: %v", links[1])
	}
}

// TestRecordLLMCall_RegistryStateIndependent guards DEX-287 Block 1 / Suggest 3:
// callers must be able to pin cost_confidence on RecordLLMCall (e.g. failure
// events) and merge per-call Details (e.g. query_index) regardless of whether
// the model exists in the pricing registry. Without this, failure-event
// confidence flips Unknown→Computed the moment a model gets added to the
// pricing map, silently breaking dashboards that group on confidence.
func TestRecordLLMCall_RegistryStateIndependent(t *testing.T) {
	t.Run("model_unknown_to_registry", func(t *testing.T) {
		tr := newTestTracker(t)
		_, tt := tr.StartTask(context.Background(), "test_failure_unknown")

		err := tt.RecordLLMCall("minimax", "MiniMax-M2.7", 0, 0,
			WithErrorType("timeout"),
			WithLatency(150),
			WithCostConfidence(CostConfidenceUnknown),
			WithDetails(map[string]interface{}{"query_index": 5}),
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
		if len(events) != 1 {
			t.Fatalf("expected 1 event, got %d", len(events))
		}
		ev := events[0]
		if ev.CostConfidence != CostConfidenceUnknown {
			t.Errorf("expected cost_confidence=unknown, got %s", ev.CostConfidence)
		}
		if ev.Details["query_index"] != 5 {
			t.Errorf("expected details.query_index=5, got %v", ev.Details["query_index"])
		}
		if ev.ErrorType != "timeout" {
			t.Errorf("expected error_type=timeout, got %s", ev.ErrorType)
		}
	})

	t.Run("override_wins_over_registry_match", func(t *testing.T) {
		tr := newTestTracker(t)
		// Inject a custom pricing entry so the engine would normally yield
		// CostConfidenceComputed for this model. The override must still pin
		// the failure event to Unknown.
		tr.Pricing().SetCustomPricing(
			"MiniMax-M2.7",
			decimal.RequireFromString("0.001"),
			decimal.RequireFromString("0.002"),
		)
		_, tt := tr.StartTask(context.Background(), "test_failure_override_wins")

		err := tt.RecordLLMCall("minimax", "MiniMax-M2.7", 0, 0,
			WithErrorType("timeout"),
			WithCostConfidence(CostConfidenceUnknown),
			WithPricingSource(PricingSourceUnknown),
			WithDetails(map[string]interface{}{"query_index": 7}),
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
		if len(events) != 1 {
			t.Fatalf("expected 1 event, got %d", len(events))
		}
		ev := events[0]
		if ev.CostConfidence != CostConfidenceUnknown {
			t.Errorf("override should pin cost_confidence=unknown, got %s", ev.CostConfidence)
		}
		if ev.PricingSource != PricingSourceUnknown {
			t.Errorf("override should pin pricing_source=unknown, got %s", ev.PricingSource)
		}
		if ev.Details["query_index"] != 7 {
			t.Errorf("expected details.query_index=7, got %v", ev.Details["query_index"])
		}
	})
}

// TestRecordLLMCall_DefaultsPreserved guards that without explicit overrides
// the auto-pricing path still owns confidence/source — i.e. the override
// block in RecordLLMCall is strictly additive and zero-valued options are
// no-ops.
func TestRecordLLMCall_DefaultsPreserved(t *testing.T) {
	tr := newTestTracker(t)
	tr.Pricing().SetCustomPricing(
		"gpt-4o",
		decimal.RequireFromString("0.001"),
		decimal.RequireFromString("0.002"),
	)
	_, tt := tr.StartTask(context.Background(), "test_no_override")

	err := tt.RecordLLMCall("openai", "gpt-4o", 1000, 500,
		WithErrorType("rate_limit"),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	events, _ := tr.Buffer().QueryEvents(tt.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.CostConfidence != CostConfidenceComputed {
		t.Errorf("expected auto-derived cost_confidence=computed, got %s", ev.CostConfidence)
	}
	if ev.PricingSource != PricingSourceCustom {
		t.Errorf("expected auto-derived pricing_source=custom, got %s", ev.PricingSource)
	}
}

// Ensure unused imports don't cause issues -- these are used in the test file.
var _ = pricing.NewRateRegistry
var _ = uuid.New

// TestRecordLLMCall_RetryHeuristicFiresOnErrorType is the regression test for
// the 🔴 bug where RecordLLMCall wrote error_type to event.ErrorType while the
// heuristic engine reads Details["error_type"] — so WithErrorType-based retry
// detection never fired.
func TestRecordLLMCall_RetryHeuristicFiresOnErrorType(t *testing.T) {
	mb := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{
		Buffer:                mb,
		EnableRetryHeuristics: true,
	})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	_, tt := tr.StartTask(context.Background(), "resolve_ticket")

	// Call 1 fails with a transient error.
	if err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithErrorType("rate_limit")); err != nil {
		t.Fatalf("record call 1: %v", err)
	}
	// Call 2 — same model, immediately after — is a retry of the failed call 1.
	if err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50); err != nil {
		t.Fatalf("record call 2: %v", err)
	}

	events, err := mb.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	retryCount := 0
	for _, e := range events {
		if e.IsRetry {
			retryCount++
			if e.RetryReason == "" {
				t.Error("a heuristically-detected retry event should carry a retry_reason")
			}
		}
	}
	if retryCount != 1 {
		t.Errorf("expected exactly 1 event flagged as a retry (heuristic field mismatch), got %d", retryCount)
	}
}
