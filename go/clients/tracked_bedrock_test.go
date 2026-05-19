package clients_test

import (
	"context"
	"errors"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/clients"
	"github.com/DexwoxBusiness/dexcost-go/core"
)

// mockBedrock satisfies the bedrockInvokeModelClient interface.
type mockBedrock struct {
	resp interface{}
	err  error
}

func (m *mockBedrock) InvokeModel(_ context.Context, _ interface{}) (interface{}, error) {
	return m.resp, m.err
}

// TestTrackedBedrock_WithExplicitTask — happy path with explicit task.
func TestTrackedBedrock_WithExplicitTask(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockBedrock{
		resp: map[string]interface{}{
			"modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
			"body": map[string]interface{}{
				"usage": map[string]interface{}{
					"input_tokens":  120,
					"output_tokens": 45,
				},
			},
		},
	}

	tracked := clients.NewTrackedBedrock(mock, tracker, engine)
	ctx, tt := tracker.StartTask(context.Background(), "test_bedrock")

	resp, err := tracked.InvokeModel(ctx, nil)
	if err != nil {
		t.Fatalf("InvokeModel: %v", err)
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
	if ev.Provider != "aws_bedrock" {
		t.Errorf("provider: got %s want aws_bedrock", ev.Provider)
	}
	if ev.Model != "claude-3-5-sonnet-20241022-v2:0" {
		t.Errorf("model: got %s want claude-3-5-sonnet-20241022-v2:0", ev.Model)
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

// TestTrackedBedrock_AutoTask — when no explicit task is in ctx, the wrapper
// should create + finalize an auto-task internally.
func TestTrackedBedrock_AutoTask(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockBedrock{
		resp: map[string]interface{}{
			"modelId": "amazon.titan-text-express-v1",
			"body": map[string]interface{}{
				"inputTextTokenCount": 80,
				"results": []interface{}{
					map[string]interface{}{"tokenCount": 30},
				},
			},
		},
	}

	tracked := clients.NewTrackedBedrock(mock, tracker, engine)
	ctx := context.Background()

	resp, err := tracked.InvokeModel(ctx, nil)
	if err != nil {
		t.Fatalf("InvokeModel: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
}

// TestTrackedBedrock_Error — when the inner client returns an error, the
// wrapper still records a failed event with cost_confidence=unknown.
func TestTrackedBedrock_Error(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockBedrock{err: errors.New("throttling exception")}
	tracked := clients.NewTrackedBedrock(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_bedrock_err")

	_, err := tracked.InvokeModel(ctx, nil)
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
	if events[0].Provider != "aws_bedrock" {
		t.Errorf("provider on failure: got %s want aws_bedrock", events[0].Provider)
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown for failure, got %s", events[0].CostConfidence)
	}
}

// TestTrackedBedrock_InvalidInner — passing a non-bedrockInvokeModelClient
// inner client must fail loudly rather than silently no-op.
func TestTrackedBedrock_InvalidInner(t *testing.T) {
	tracker, _ := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	tracked := clients.NewTrackedBedrock("not-a-client", tracker, engine)
	ctx, _ := tracker.StartTask(context.Background(), "test_invalid")

	_, err := tracked.InvokeModel(ctx, nil)
	if err == nil {
		t.Fatal("expected error for invalid inner client")
	}
}

// TestTrackedBedrock_NonMapResponse — when the inner client returns something
// that is not a map[string]interface{}, the wrapper should still return the
// raw response but record a minimal event.
func TestTrackedBedrock_NonMapResponse(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockBedrock{resp: "not-a-map"}
	tracked := clients.NewTrackedBedrock(mock, tracker, engine)

	ctx, tt := tracker.StartTask(context.Background(), "test_nonmap")

	resp, err := tracked.InvokeModel(ctx, nil)
	if err != nil {
		t.Fatalf("InvokeModel: %v", err)
	}
	if resp != "not-a-map" {
		t.Fatalf("expected raw response to be passed through")
	}

	events, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown for non-map response, got %s", events[0].CostConfidence)
	}
}

// TestTrackedBedrock_RecordResponseError — when the response map is missing
// modelId, RecordBedrockResponse returns an error and the wrapper records a
// minimal failed event.
func TestTrackedBedrock_RecordResponseError(t *testing.T) {
	tracker, buf := newTrackerAndBuffer(t)
	engine := newTestPricingEngine(t)

	mock := &mockBedrock{
		resp: map[string]interface{}{
			"body": map[string]interface{}{
				"usage": map[string]interface{}{
					"input_tokens":  10,
					"output_tokens": 5,
				},
			},
		},
	}

	tracked := clients.NewTrackedBedrock(mock, tracker, engine)
	ctx, tt := tracker.StartTask(context.Background(), "test_record_err")

	resp, err := tracked.InvokeModel(ctx, nil)
	if err != nil {
		t.Fatalf("InvokeModel: %v", err)
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
	if events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected cost_confidence=unknown for record error, got %s", events[0].CostConfidence)
	}
}
