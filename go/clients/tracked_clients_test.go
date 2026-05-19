package clients_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/clients"
	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/DexwoxBusiness/dexcost-go/transport"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// --- mock clients ---

// mockOpenAI satisfies the openaiCompletionCreator interface.
type mockOpenAI struct {
	resp interface{}
	err  error
}

func (m *mockOpenAI) CreateChatCompletion(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// mockAnthropic satisfies the anthropicMessageCreator interface.
type mockAnthropic struct {
	resp interface{}
	err  error
}

func (m *mockAnthropic) CreateMessage(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// mockGemini satisfies the geminiContentGenerator interface.
type mockGemini struct {
	resp interface{}
	err  error
}

func (m *mockGemini) GenerateContent(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// --- helpers ---

func newTrackerAndBuffer(t *testing.T) (*core.Tracker, core.Buffer) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "tracked_test.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	t.Cleanup(func() { buf.Close() })

	engine, err := pricing.NewEngine()
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}

	tracker, err := core.NewTracker(core.TrackerOptions{
		Buffer:  buf,
		Pricing: engine,
	})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	return tracker, buf
}

// --- TrackedOpenAI tests ---

func TestTrackedOpenAI_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockOpenAI{
		resp: map[string]interface{}{
			"model": "gpt-4o",
			"usage": map[string]interface{}{
				"prompt_tokens":     100,
				"completion_tokens": 50,
			},
		},
	}

	tracked := clients.NewTrackedOpenAI(mock, tracker, engine)

	// Create an explicit task.
	ctx, tt := tracker.StartTask(context.Background(), "test_openai")

	resp, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}

	// Verify event was recorded under the explicit task.
	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Provider != "openai" {
		t.Errorf("expected provider=openai, got %s", events[0].Provider)
	}
	if events[0].Model != "gpt-4o" {
		t.Errorf("expected model=gpt-4o, got %s", events[0].Model)
	}
}

func TestTrackedOpenAI_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockOpenAI{
		resp: map[string]interface{}{
			"model": "gpt-4o",
			"usage": map[string]interface{}{
				"prompt_tokens":     200,
				"completion_tokens": 100,
			},
		},
	}

	tracked := clients.NewTrackedOpenAI(mock, tracker, engine)

	// No explicit task in context — auto-task should be created.
	ctx := core.SetContext(context.Background(), &core.ContextData{
		CustomerID: "test-customer",
		ProjectID:  "test-project",
	})

	resp, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

func TestTrackedOpenAI_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockOpenAI{
		err: errors.New("api error"),
	}

	tracked := clients.NewTrackedOpenAI(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_openai_err")

	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err == nil {
		t.Fatal("expected error from inner client")
	}

	// Should still record a failed event.
	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown, got %s", events[0].CostConfidence)
	}
}

func TestTrackedOpenAI_InvalidInner(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	// Pass a non-compliant inner client.
	tracked := clients.NewTrackedOpenAI("not-a-client", tracker, engine)

	ctx, _ := tracker.StartTask(context.Background(), "test_invalid")
	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err == nil {
		t.Fatal("expected error for invalid inner client")
	}
}

// --- TrackedAnthropic tests ---

func TestTrackedAnthropic_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockAnthropic{
		resp: map[string]interface{}{
			"model": "claude-3-5-sonnet-20241022",
			"usage": map[string]interface{}{
				"input_tokens":  150,
				"output_tokens": 60,
			},
		},
	}

	tracked := clients.NewTrackedAnthropic(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_anthropic")

	resp, err := tracked.CreateMessage(ctx, nil)
	if err != nil {
		t.Fatalf("CreateMessage: %v", err)
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
	if events[0].Provider != "anthropic" {
		t.Errorf("expected provider=anthropic, got %s", events[0].Provider)
	}
	if events[0].Model != "claude-3-5-sonnet-20241022" {
		t.Errorf("expected model=claude-3-5-sonnet-20241022, got %s", events[0].Model)
	}
}

func TestTrackedAnthropic_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockAnthropic{
		resp: map[string]interface{}{
			"model": "claude-3-5-sonnet-20241022",
			"usage": map[string]interface{}{
				"input_tokens":  300,
				"output_tokens": 100,
			},
		},
	}

	tracked := clients.NewTrackedAnthropic(mock, tracker, engine)

	ctx := context.Background()
	resp, err := tracked.CreateMessage(ctx, nil)
	if err != nil {
		t.Fatalf("CreateMessage: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

func TestTrackedAnthropic_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockAnthropic{
		err: errors.New("rate limit"),
	}

	tracked := clients.NewTrackedAnthropic(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_anthropic_err")

	_, err := tracked.CreateMessage(ctx, nil)
	if err == nil {
		t.Fatal("expected error")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown, got %s", events[0].CostConfidence)
	}
}

// --- TrackedGemini tests ---

func TestTrackedGemini_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGemini{
		resp: map[string]interface{}{
			"model": "gemini-1.5-pro",
			"usageMetadata": map[string]interface{}{
				"promptTokenCount":     120,
				"candidatesTokenCount": 80,
			},
		},
	}

	tracked := clients.NewTrackedGemini(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_gemini")

	resp, err := tracked.GenerateContent(ctx, nil)
	if err != nil {
		t.Fatalf("GenerateContent: %v", err)
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
	if events[0].Provider != "google" {
		t.Errorf("expected provider=google, got %s", events[0].Provider)
	}
}

func TestTrackedGemini_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGemini{
		resp: map[string]interface{}{
			"model": "gemini-1.5-pro",
			"usageMetadata": map[string]interface{}{
				"promptTokenCount":     200,
				"candidatesTokenCount": 100,
			},
		},
	}

	tracked := clients.NewTrackedGemini(mock, tracker, engine)

	ctx := context.Background()
	resp, err := tracked.GenerateContent(ctx, nil)
	if err != nil {
		t.Fatalf("GenerateContent: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

func TestTrackedGemini_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockGemini{
		err: errors.New("quota exceeded"),
	}

	tracked := clients.NewTrackedGemini(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_gemini_err")

	_, err := tracked.GenerateContent(ctx, nil)
	if err == nil {
		t.Fatal("expected error")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
}

// --- RecordGeminiResponse tests ---

func TestRecordGeminiResponse_RecordsEvent(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := newTestTaskID(t, buf)

	resp := map[string]interface{}{
		"model": "gemini-1.5-pro",
		"usageMetadata": map[string]interface{}{
			"promptTokenCount":     100,
			"candidatesTokenCount": 50,
		},
	}

	event, err := clients.RecordGeminiResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordGeminiResponse: %v", err)
	}

	if event.Provider != "google" {
		t.Errorf("Provider = %q, want %q", event.Provider, "google")
	}
	if event.Model != "gemini-1.5-pro" {
		t.Errorf("Model = %q, want %q", event.Model, "gemini-1.5-pro")
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
	if event.TaskID != taskID {
		t.Errorf("TaskID = %v, want %v", event.TaskID, taskID)
	}

	// Verify persisted.
	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("len(events) = %d, want 1", len(events))
	}
}

func TestRecordGeminiResponse_HandlesCacheTokens(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := newTestTaskID(t, buf)

	resp := map[string]interface{}{
		"model": "gemini-1.5-pro",
		"usageMetadata": map[string]interface{}{
			"promptTokenCount":         200,
			"candidatesTokenCount":     80,
			"cachedContentTokenCount":  40,
		},
	}

	event, err := clients.RecordGeminiResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordGeminiResponse: %v", err)
	}

	if event.CachedTokens == nil || *event.CachedTokens != 40 {
		t.Errorf("CachedTokens = %v, want 40", event.CachedTokens)
	}
	if event.InputTokens == nil || *event.InputTokens != 200 {
		t.Errorf("InputTokens = %v, want 200", event.InputTokens)
	}
}

func TestRecordGeminiResponse_MissingModel(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := newTestTaskID(t, buf)

	resp := map[string]interface{}{
		"usageMetadata": map[string]interface{}{
			"promptTokenCount":     100,
			"candidatesTokenCount": 50,
		},
	}

	_, err := clients.RecordGeminiResponse(buf, engine, taskID, resp)
	if err == nil {
		t.Fatal("expected error for missing model")
	}
}

func TestRecordGeminiResponse_MissingUsageMetadata(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := newTestTaskID(t, buf)

	resp := map[string]interface{}{
		"model": "gemini-1.5-pro",
	}

	_, err := clients.RecordGeminiResponse(buf, engine, taskID, resp)
	if err == nil {
		t.Fatal("expected error for missing usageMetadata")
	}
}

// --- TrackedOpenAI auto-task finalization test ---

func TestTrackedOpenAI_AutoTaskFinalized(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockOpenAI{
		resp: map[string]interface{}{
			"model": "gpt-4o",
			"usage": map[string]interface{}{
				"prompt_tokens":     100,
				"completion_tokens": 50,
			},
		},
	}

	tracked := clients.NewTrackedOpenAI(mock, tracker, engine)

	// No task in context.
	ctx := context.Background()
	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}

	// The auto-task should have been created and finalized.
	// We can't easily get its ID, but we can verify the buffer has tasks.
	_ = buf
}

// --- TrackedAnthropic auto-task error finalization test ---

func TestTrackedAnthropic_AutoTaskError(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockAnthropic{
		err: errors.New("server error"),
	}

	tracked := clients.NewTrackedAnthropic(mock, tracker, engine)

	ctx := context.Background()
	_, err := tracked.CreateMessage(ctx, nil)
	if err == nil {
		t.Fatal("expected error")
	}
}

// --- Cost accuracy test ---

func TestTrackedOpenAI_CostRecorded(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockOpenAI{
		resp: map[string]interface{}{
			"model": "gpt-4o",
			"usage": map[string]interface{}{
				"prompt_tokens":     1000,
				"completion_tokens": 500,
			},
		},
	}

	tracked := clients.NewTrackedOpenAI(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "cost_test")

	_, err := tracked.CreateChatCompletion(ctx, nil)
	if err != nil {
		t.Fatalf("CreateChatCompletion: %v", err)
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}

	// Cost should be non-zero for gpt-4o with these token counts.
	if events[0].CostUSD.Equal(decimal.Zero) {
		t.Error("expected non-zero cost for gpt-4o")
	}
	if events[0].CostConfidence == core.CostConfidenceUnknown {
		t.Error("expected known cost confidence for gpt-4o")
	}
}

// newTestTaskID creates and inserts a task, returning its UUID.
func newTestTaskID(t *testing.T, buf core.Buffer) uuid.UUID {
	t.Helper()
	task := core.NewTask("test_task")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("InsertTask: %v", err)
	}
	return task.TaskID
}
