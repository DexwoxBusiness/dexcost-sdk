package dexcost

import (
	"context"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// TestAllSupportedInstruments verifies the instrument list covers every
// top-level Wrap* / Record* entry point (Go SDK parity audit, Task 7).
func TestAllSupportedInstruments(t *testing.T) {
	want := []string{"openai", "anthropic", "gemini", "bedrock", "cohere", "groq", "litellm", "langchain"}
	if len(ALL_SUPPORTED_INSTRUMENTS) != len(want) {
		t.Fatalf("expected %d instruments, got %d: %v", len(want), len(ALL_SUPPORTED_INSTRUMENTS), ALL_SUPPORTED_INSTRUMENTS)
	}
	have := make(map[string]bool, len(ALL_SUPPORTED_INSTRUMENTS))
	for _, n := range ALL_SUPPORTED_INSTRUMENTS {
		have[n] = true
	}
	for _, n := range want {
		if !have[n] {
			t.Errorf("ALL_SUPPORTED_INSTRUMENTS missing %q", n)
		}
	}
}

// TestProviderWrappers verifies the new top-level provider wrappers construct
// a non-nil tracked client (Go SDK parity audit, Task 7).
func TestProviderWrappers(t *testing.T) {
	initLocal(t)
	if WrapBedrock(nil) == nil {
		t.Error("WrapBedrock returned nil")
	}
	if WrapCohere(nil) == nil {
		t.Error("WrapCohere returned nil")
	}
	if WrapGroq(nil) == nil {
		t.Error("WrapGroq returned nil")
	}
}

// TestTaskEventFromDictRoundTrip verifies the top-level deserialization helpers
// round-trip a serialized Task/Event (Go SDK parity audit, Task 9).
func TestTaskEventFromDictRoundTrip(t *testing.T) {
	task := core.NewTask("resolve_ticket")
	task.CustomerID = "acme"
	got, err := TaskFromDict(task.ToDict())
	if err != nil {
		t.Fatalf("TaskFromDict: %v", err)
	}
	if got.TaskID != task.TaskID || got.CustomerID != "acme" {
		t.Errorf("Task round-trip mismatch: %+v vs %+v", got, task)
	}

	event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
	event.Model = "gpt-4o"
	gotEvent, err := EventFromDict(event.ToDict())
	if err != nil {
		t.Fatalf("EventFromDict: %v", err)
	}
	if gotEvent.EventID != event.EventID || gotEvent.Model != "gpt-4o" {
		t.Errorf("Event round-trip mismatch: %+v vs %+v", gotEvent, event)
	}
}

// TestRecordLiteLLM verifies the top-level LiteLLM helper records a cost event
// against the active task (Go SDK parity audit, Task 8).
func TestRecordLiteLLM(t *testing.T) {
	initLocal(t)
	ctx, task := StartTask(context.Background(), "litellm_task")
	event, err := RecordLiteLLM(ctx, map[string]interface{}{
		"model": "openai/gpt-4o",
		"usage": map[string]interface{}{
			"prompt_tokens":     float64(100),
			"completion_tokens": float64(50),
		},
	})
	if err != nil {
		t.Fatalf("RecordLiteLLM: %v", err)
	}
	if event.Provider != "openai" || event.Model != "openai/gpt-4o" {
		t.Errorf("unexpected event provider/model: %q / %q", event.Provider, event.Model)
	}
	if event.CostUSD.IsZero() {
		t.Error("expected non-zero auto-priced cost for gpt-4o")
	}
	_ = task.End(StatusSuccess)
}

// TestRecordLiteLLM_NoTask verifies the helper errors without an active task.
func TestRecordLiteLLM_NoTask(t *testing.T) {
	initLocal(t)
	_, err := RecordLiteLLM(context.Background(), map[string]interface{}{"model": "gpt-4o"})
	if err == nil {
		t.Error("expected error when no active task")
	}
}

// TestSessionGroupingByIdentity verifies consecutive anonymous calls with the
// same attribution identity reuse one session task (Go SDK parity audit,
// Task 4) and a different identity gets its own task.
func TestSessionGroupingByIdentity(t *testing.T) {
	sm := NewSessionManager(30 * time.Second)
	defer sm.Clear()

	acme := core.SetContext(context.Background(), &core.ContextData{CustomerID: "acme"})
	t1 := sm.GetOrCreateSessionForIdentity(acme, "http_request", nil)
	t2 := sm.GetOrCreateSessionForIdentity(acme, "http_request", nil)
	if t1.TaskID != t2.TaskID {
		t.Errorf("expected same session task for identical identity, got %s and %s", t1.TaskID, t2.TaskID)
	}

	globex := core.SetContext(context.Background(), &core.ContextData{CustomerID: "globex"})
	t3 := sm.GetOrCreateSessionForIdentity(globex, "http_request", nil)
	if t3.TaskID == t1.TaskID {
		t.Error("expected distinct session task for a different identity")
	}
	if sm.ActiveSessionCount() != 2 {
		t.Errorf("expected 2 active sessions, got %d", sm.ActiveSessionCount())
	}
}

// TestInit_RetryHeuristicsReachable verifies the retry heuristic engine can be
// turned on through Init() — previously it was unreachable (no Config field).
func TestInit_RetryHeuristicsReachable(t *testing.T) {
	dir := t.TempDir()
	Close()
	if err := Init(Config{Storage: "local", BufferDir: dir, EnableRetryHeuristics: true}); err != nil {
		t.Fatalf("Init: %v", err)
	}
	defer Close()

	_, tt := StartTask(context.Background(), "resolve_ticket")
	if err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50, WithErrorType("rate_limit")); err != nil {
		t.Fatalf("record call 1: %v", err)
	}
	if err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50); err != nil {
		t.Fatalf("record call 2: %v", err)
	}

	events, err := Tracker().Buffer().QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	retryCount := 0
	for _, e := range events {
		if e.IsRetry {
			retryCount++
		}
	}
	if retryCount != 1 {
		t.Errorf("retry detection did not fire via Init(EnableRetryHeuristics=true): got %d retries", retryCount)
	}
}
