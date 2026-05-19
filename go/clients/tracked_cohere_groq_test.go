package clients_test

import (
	"context"
	"errors"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/clients"
	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/google/uuid"
)

// --- mock clients ---

// mockCohere satisfies the cohereChatCreator interface (Chat method).
type mockCohere struct {
	resp interface{}
	err  error
}

func (m *mockCohere) Chat(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// mockGroq satisfies the openaiCompletionCreator interface — Groq is wire-
// compatible with OpenAI, so the tracked Groq wrapper relies on the same
// CreateChatCompletion signature.
type mockGroq struct {
	resp interface{}
	err  error
}

func (m *mockGroq) CreateChatCompletion(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// --- TrackedCohere tests ---

// TestTrackedCohere_WithExplicitTask — the wrapper should attach the recorded
// event to the explicit task in context and stamp provider="cohere" with the
// model returned by the response.
func TestTrackedCohere_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockCohere{
		resp: map[string]interface{}{
			"model": "command-r-plus",
			"meta": map[string]interface{}{
				"billed_units": map[string]interface{}{
					"input_tokens":  120,
					"output_tokens": 45,
				},
			},
		},
	}

	tracked := clients.NewTrackedCohere(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_cohere")

	resp, err := tracked.Chat(ctx, nil)
	if err != nil {
		t.Fatalf("Chat: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.Provider != "cohere" {
		t.Errorf("provider: got %s want cohere", ev.Provider)
	}
	if ev.Model != "command-r-plus" {
		t.Errorf("model: got %s want command-r-plus", ev.Model)
	}
	if ev.InputTokens == nil || *ev.InputTokens != 120 {
		t.Errorf("input_tokens: got %v want 120", ev.InputTokens)
	}
	if ev.OutputTokens == nil || *ev.OutputTokens != 45 {
		t.Errorf("output_tokens: got %v want 45", ev.OutputTokens)
	}
	if ev.LatencyMs == nil {
		t.Error("expected latency_ms to be populated")
	}
}

// TestTrackedCohere_AutoTask — when no explicit task is in ctx, the wrapper
// should create + finalize an auto-task internally.
func TestTrackedCohere_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockCohere{
		resp: map[string]interface{}{
			"model": "command-r",
			"meta": map[string]interface{}{
				"billed_units": map[string]interface{}{
					"input_tokens":  80,
					"output_tokens": 30,
				},
			},
		},
	}

	tracked := clients.NewTrackedCohere(mock, tracker, engine)
	ctx := context.Background()

	resp, err := tracked.Chat(ctx, nil)
	if err != nil {
		t.Fatalf("Chat: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

// TestTrackedCohere_Error — when the inner client returns an error, the
// wrapper still records a failed event with cost_confidence=unknown.
func TestTrackedCohere_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockCohere{err: errors.New("rate limit")}
	tracked := clients.NewTrackedCohere(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_cohere_err")

	_, err := tracked.Chat(ctx, nil)
	if err == nil {
		t.Fatal("expected error from inner client")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Provider != "cohere" {
		t.Errorf("provider on failure: got %s want cohere", events[0].Provider)
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown for failure, got %s", events[0].CostConfidence)
	}
}

// TestTrackedCohere_InvalidInner — passing a non-cohereChatCreator inner
// client must fail loudly rather than silently no-op.
func TestTrackedCohere_InvalidInner(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	tracked := clients.NewTrackedCohere("not-a-client", tracker, engine)
	ctx, _ := tracker.StartTask(context.Background(), "test_invalid")

	_, err := tracked.Chat(ctx, nil)
	if err == nil {
		t.Fatal("expected error for invalid inner client")
	}
}

// --- TrackedGroq tests ---

// TestTrackedGroq_WithExplicitTask — Groq is wire-compatible with OpenAI but
// the wrapper must stamp provider="groq" so dashboards can attribute spend.
func TestTrackedGroq_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGroq{
		resp: map[string]interface{}{
			"model": "llama-3.1-70b-versatile",
			"usage": map[string]interface{}{
				"prompt_tokens":     100,
				"completion_tokens": 50,
			},
		},
	}

	tracked := clients.NewTrackedGroq(mock, tracker, engine)
	ctx, tt := tracker.StartTask(context.Background(), "test_groq")

	resp, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.Provider != "groq" {
		t.Errorf("provider: got %s want groq", ev.Provider)
	}
	if ev.Model != "llama-3.1-70b-versatile" {
		t.Errorf("model: got %s want llama-3.1-70b-versatile", ev.Model)
	}
	if ev.InputTokens == nil || *ev.InputTokens != 100 {
		t.Errorf("input_tokens: got %v want 100", ev.InputTokens)
	}
	if ev.OutputTokens == nil || *ev.OutputTokens != 50 {
		t.Errorf("output_tokens: got %v want 50", ev.OutputTokens)
	}
	if ev.LatencyMs == nil {
		t.Error("expected latency_ms to be populated")
	}
}

// TestTrackedGroq_AutoTask — auto-task path mirrors OpenAI's behavior.
func TestTrackedGroq_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGroq{
		resp: map[string]interface{}{
			"model": "llama-3.1-8b-instant",
			"usage": map[string]interface{}{
				"prompt_tokens":     30,
				"completion_tokens": 15,
			},
		},
	}

	tracked := clients.NewTrackedGroq(mock, tracker, engine)
	ctx := context.Background()

	resp, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

// TestTrackedGroq_Error — inner-client error still produces a failed event.
func TestTrackedGroq_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGroq{err: errors.New("api error")}
	tracked := clients.NewTrackedGroq(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_groq_err")

	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err == nil {
		t.Fatal("expected error from inner client")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Provider != "groq" {
		t.Errorf("provider on failure: got %s want groq", events[0].Provider)
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown for failure, got %s", events[0].CostConfidence)
	}
}

// TestTrackedGroq_InvalidInner — inner client without CreateChatCompletion
// must surface a clear error rather than panicking on type assertion.
func TestTrackedGroq_InvalidInner(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	tracked := clients.NewTrackedGroq(struct{}{}, tracker, engine)
	ctx, _ := tracker.StartTask(context.Background(), "test_invalid")

	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err == nil {
		t.Fatal("expected error for invalid inner client")
	}
}

// --- RecordCohereResponse tests ---

// TestRecordCohereResponse_RecordsEvent — happy path: model + meta.billed_units
// produces a valid llm_call event.
func TestRecordCohereResponse_RecordsEvent(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"model": "command-r-plus",
		"meta": map[string]interface{}{
			"billed_units": map[string]interface{}{
				"input_tokens":  120,
				"output_tokens": 45,
			},
		},
	}

	event, err := clients.RecordCohereResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordCohereResponse: %v", err)
	}

	if event.Provider != "cohere" {
		t.Errorf("Provider = %q, want cohere", event.Provider)
	}
	if event.Model != "command-r-plus" {
		t.Errorf("Model = %q, want command-r-plus", event.Model)
	}
	if event.EventType != core.EventTypeLLMCall {
		t.Errorf("EventType = %q, want %q", event.EventType, core.EventTypeLLMCall)
	}
	if event.InputTokens == nil || *event.InputTokens != 120 {
		t.Errorf("InputTokens = %v, want 120", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 45 {
		t.Errorf("OutputTokens = %v, want 45", event.OutputTokens)
	}
	if event.TaskID != taskID {
		t.Errorf("TaskID = %v, want %v", event.TaskID, taskID)
	}

	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("len(events) = %d, want 1", len(events))
	}
}

// TestRecordCohereResponse_DefaultsModel — when the response omits "model",
// the helper should fall back to "command-r-plus" (matches Python instrument).
func TestRecordCohereResponse_DefaultsModel(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"meta": map[string]interface{}{
			"billed_units": map[string]interface{}{
				"input_tokens":  10,
				"output_tokens": 5,
			},
		},
	}

	event, err := clients.RecordCohereResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordCohereResponse: %v", err)
	}
	if event.Model != "command-r-plus" {
		t.Errorf("default model: got %s want command-r-plus", event.Model)
	}
}

// TestRecordCohereResponse_MissingMetaIsZero — without meta.billed_units the
// helper should record zero tokens rather than erroring out.
func TestRecordCohereResponse_MissingMetaIsZero(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{"model": "command-r"}

	event, err := clients.RecordCohereResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordCohereResponse: %v", err)
	}
	if event.InputTokens == nil || *event.InputTokens != 0 {
		t.Errorf("InputTokens: got %v want 0", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 0 {
		t.Errorf("OutputTokens: got %v want 0", event.OutputTokens)
	}
}

// --- RecordGroqResponse tests ---

// TestRecordGroqResponse_RecordsEvent — happy path mirrors OpenAI but stamps
// provider="groq".
func TestRecordGroqResponse_RecordsEvent(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"model": "llama-3.1-70b-versatile",
		"usage": map[string]interface{}{
			"prompt_tokens":     100,
			"completion_tokens": 50,
		},
	}

	event, err := clients.RecordGroqResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordGroqResponse: %v", err)
	}

	if event.Provider != "groq" {
		t.Errorf("Provider = %q, want groq", event.Provider)
	}
	if event.Model != "llama-3.1-70b-versatile" {
		t.Errorf("Model = %q, want llama-3.1-70b-versatile", event.Model)
	}
	if event.EventType != core.EventTypeLLMCall {
		t.Errorf("EventType = %q, want %q", event.EventType, core.EventTypeLLMCall)
	}
	if event.InputTokens == nil || *event.InputTokens != 100 {
		t.Errorf("InputTokens = %v, want 100", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 50 {
		t.Errorf("OutputTokens = %v, want 50", event.OutputTokens)
	}

	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("len(events) = %d, want 1", len(events))
	}
}

// TestRecordGroqResponse_MissingModel — the model field is required by the
// helper (unlike Cohere). Confirm we surface a clear error instead of writing
// an event with empty model.
func TestRecordGroqResponse_MissingModel(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"usage": map[string]interface{}{
			"prompt_tokens":     100,
			"completion_tokens": 50,
		},
	}

	_, err := clients.RecordGroqResponse(buf, engine, taskID, resp)
	if err == nil {
		t.Fatal("expected error for missing model field")
	}
}

// TestRecordGroqResponse_MissingUsage — ditto for usage map.
func TestRecordGroqResponse_MissingUsage(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{"model": "llama-3.1-8b-instant"}

	_, err := clients.RecordGroqResponse(buf, engine, taskID, resp)
	if err == nil {
		t.Fatal("expected error for missing usage map")
	}
}
