package clients_test

import (
	"path/filepath"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/attribution"
	"github.com/DexwoxBusiness/dexcost-sdk/go/clients"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/DexwoxBusiness/dexcost-sdk/go/transport"
	"github.com/google/uuid"
)

func newTestBuffer(t *testing.T) core.Buffer {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	t.Cleanup(func() { buf.Close() })
	return buf
}

func newTestPricingEngine(t *testing.T) *pricing.Engine {
	t.Helper()
	engine, err := pricing.NewEngine()
	if err != nil {
		t.Fatalf("pricing.NewEngine: %v", err)
	}
	return engine
}

// seedTask inserts a minimal task so foreign key constraints are satisfied.
func seedTask(t *testing.T, buf core.Buffer, taskID uuid.UUID) {
	t.Helper()
	task := core.NewTask("test_task")
	task.TaskID = taskID
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("InsertTask: %v", err)
	}
}

// Test 1: RecordOpenAIResponse records correct event with cost.
func TestRecordOpenAIResponse_RecordsEvent(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"model": "gpt-4o",
		"usage": map[string]interface{}{
			"prompt_tokens":     100,
			"completion_tokens": 50,
		},
	}

	event, err := clients.RecordOpenAIResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordOpenAIResponse: %v", err)
	}

	if event.Provider != "openai" {
		t.Errorf("Provider = %q, want %q", event.Provider, "openai")
	}
	if event.Model != "gpt-4o" {
		t.Errorf("Model = %q, want %q", event.Model, "gpt-4o")
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
	if event.CostUSD.IsZero() {
		t.Error("CostUSD should be non-zero for known model gpt-4o")
	}
	if event.TaskID != taskID {
		t.Errorf("TaskID = %v, want %v", event.TaskID, taskID)
	}

	// Verify event was actually stored in the buffer.
	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("len(events) = %d, want 1", len(events))
	}
	if events[0].EventID != event.EventID {
		t.Errorf("stored event ID mismatch")
	}
}

// Test 2: RecordOpenAIResponse handles missing optional fields (no cached_tokens).
func TestRecordOpenAIResponse_MissingOptionalFields(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	// No prompt_tokens_details / cached_tokens in usage.
	resp := map[string]interface{}{
		"model": "gpt-4o-mini",
		"usage": map[string]interface{}{
			"prompt_tokens":     200,
			"completion_tokens": 80,
			// No cached_tokens field.
		},
	}

	event, err := clients.RecordOpenAIResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordOpenAIResponse: %v", err)
	}

	// CachedTokens should be nil or zero — the field is omitted.
	if event.CachedTokens != nil && *event.CachedTokens != 0 {
		t.Errorf("CachedTokens = %v, want nil or 0 when not provided", event.CachedTokens)
	}
	if event.InputTokens == nil || *event.InputTokens != 200 {
		t.Errorf("InputTokens = %v, want 200", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 80 {
		t.Errorf("OutputTokens = %v, want 80", event.OutputTokens)
	}
}

func TestRecordOpenAIResponse_ResponsesLunaBillingBuckets(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	response := map[string]interface{}{
		"id":    "resp_luna_go",
		"model": "gpt-5.6-luna",
		"usage": map[string]interface{}{
			"input_tokens": 2600,
			"input_tokens_details": map[string]interface{}{
				"cached_tokens":      2000,
				"cache_write_tokens": 400,
			},
			"output_tokens": 300,
			"output_tokens_details": map[string]interface{}{
				"reasoning_tokens": 120,
			},
		},
	}

	event, err := clients.RecordOpenAIResponse(buf, engine, taskID, response)
	if err != nil {
		t.Fatalf("RecordOpenAIResponse: %v", err)
	}
	if event.InputTokens == nil || *event.InputTokens != 2600 {
		t.Fatalf("InputTokens = %v, want 2600", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 300 {
		t.Fatalf("OutputTokens = %v, want 300", event.OutputTokens)
	}
	if event.CachedTokens == nil || *event.CachedTokens != 2000 {
		t.Fatalf("CachedTokens = %v, want 2000", event.CachedTokens)
	}
	if event.CostConfidence != core.CostConfidenceUnknown || !event.CostUSD.IsZero() {
		t.Fatalf("unknown local Luna pricing must remain an unpriced zero, got %s %s", event.CostConfidence, event.CostUSD)
	}
	if event.Details["cache_write_input_tokens"] != 400 {
		t.Fatalf("cache write detail = %v, want 400", event.Details["cache_write_input_tokens"])
	}
	if event.Details["reasoning_output_tokens"] != 120 {
		t.Fatalf("reasoning detail = %v, want 120", event.Details["reasoning_output_tokens"])
	}
	if event.Details["provider_record_id"] != "resp_luna_go" {
		t.Fatalf("provider record id = %v, want resp_luna_go", event.Details["provider_record_id"])
	}

	observation := attribution.ToObservationV3(event)
	if observation == nil {
		t.Fatal("Luna event was not representable as attribution v3")
	}
	want := []struct{ metric, quantity string }{
		{"input_tokens", "200"},
		{"cache_read_input_tokens", "2000"},
		{"cache_write_input_tokens", "400"},
		{"output_tokens", "180"},
		{"reasoning_output_tokens", "120"},
	}
	if len(observation.Usage) != len(want) {
		t.Fatalf("usage lines = %d, want %d", len(observation.Usage), len(want))
	}
	for index, expected := range want {
		if observation.Usage[index].Metric != expected.metric || observation.Usage[index].Quantity != expected.quantity {
			t.Errorf("usage[%d] = %s/%s, want %s/%s", index, observation.Usage[index].Metric, observation.Usage[index].Quantity, expected.metric, expected.quantity)
		}
	}
}

func TestRecordOpenAIResponse_RejectsOverlappingCacheBuckets(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	_, err := clients.RecordOpenAIResponse(buf, engine, taskID, map[string]interface{}{
		"model": "gpt-5.6-luna",
		"usage": map[string]interface{}{
			"input_tokens": 10,
			"input_tokens_details": map[string]interface{}{
				"cached_tokens":      9,
				"cache_write_tokens": 2,
			},
			"output_tokens": 1,
		},
	})
	if err == nil {
		t.Fatal("expected invalid OpenAI usage error")
	}
	events, queryErr := buf.QueryEvents(taskID.String())
	if queryErr != nil {
		t.Fatalf("QueryEvents: %v", queryErr)
	}
	if len(events) != 0 {
		t.Fatalf("invalid explicit helper call persisted %d event(s), want 0", len(events))
	}
}

// Test 3: RecordAnthropicResponse records correct event.
func TestRecordAnthropicResponse_RecordsEvent(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"model": "claude-3-5-sonnet-20241022",
		"usage": map[string]interface{}{
			"input_tokens":  150,
			"output_tokens": 60,
		},
	}

	event, err := clients.RecordAnthropicResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordAnthropicResponse: %v", err)
	}

	if event.Provider != "anthropic" {
		t.Errorf("Provider = %q, want %q", event.Provider, "anthropic")
	}
	if event.Model != "claude-3-5-sonnet-20241022" {
		t.Errorf("Model = %q, want %q", event.Model, "claude-3-5-sonnet-20241022")
	}
	if event.EventType != core.EventTypeLLMCall {
		t.Errorf("EventType = %q, want %q", event.EventType, core.EventTypeLLMCall)
	}
	if event.InputTokens == nil || *event.InputTokens != 150 {
		t.Errorf("InputTokens = %v, want 150", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 60 {
		t.Errorf("OutputTokens = %v, want 60", event.OutputTokens)
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

// Test 4: RecordAnthropicResponse handles cache_read_input_tokens.
func TestRecordAnthropicResponse_HandlesCacheTokens(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	resp := map[string]interface{}{
		"model": "claude-3-5-sonnet-20241022",
		"usage": map[string]interface{}{
			"input_tokens":            300,
			"output_tokens":           100,
			"cache_read_input_tokens": 50,
		},
	}

	event, err := clients.RecordAnthropicResponse(buf, engine, taskID, resp)
	if err != nil {
		t.Fatalf("RecordAnthropicResponse: %v", err)
	}

	if event.CachedTokens == nil || *event.CachedTokens != 50 {
		t.Errorf("CachedTokens = %v, want 50", event.CachedTokens)
	}
	if event.InputTokens == nil || *event.InputTokens != 300 {
		t.Errorf("InputTokens = %v, want 300", event.InputTokens)
	}
	if event.OutputTokens == nil || *event.OutputTokens != 100 {
		t.Errorf("OutputTokens = %v, want 100", event.OutputTokens)
	}
}
