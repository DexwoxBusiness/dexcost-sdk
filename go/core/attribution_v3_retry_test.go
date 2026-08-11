package core

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

func TestAttributionV3RetryChainPersistsRootAndIncreasingAttempts(t *testing.T) {
	tracker := newTestTrackerWithHeuristics(t, 30, 0.1)
	_, task := tracker.StartTask(context.Background(), "retry-chain")
	cost := decimal.RequireFromString("0.05")
	for attempt := 0; attempt < 3; attempt++ {
		if err := task.RecordLLMCall(
			"openai", "gpt-4o", 100, 50,
			WithCost(cost), WithErrorType("rate_limit"),
		); err != nil {
			t.Fatal(err)
		}
	}

	events, err := tracker.Buffer().QueryEvents(task.Task.TaskID.String())
	if err != nil || len(events) != 3 {
		t.Fatalf("query events: %v count=%d", err, len(events))
	}
	first, second, third := events[0], events[1], events[2]
	if second.RetryOf == nil || *second.RetryOf != first.EventID {
		t.Fatalf("attempt 2 retry_of = %v, want %s", second.RetryOf, first.EventID)
	}
	if third.RetryOf == nil || *third.RetryOf != second.EventID {
		t.Fatalf("attempt 3 retry_of = %v, want %s", third.RetryOf, second.EventID)
	}
	if got := third.Details["attribution_operation_id"]; got != first.EventID.String() {
		t.Fatalf("attempt 3 operation root = %v, want %s", got, first.EventID)
	}
	if got, ok := positiveDetailInt(third.Details["attribution_attempt_number"]); !ok || got != 3 {
		t.Fatalf("attempt 3 number = %v", third.Details["attribution_attempt_number"])
	}
}

func TestMarkNotRetryResetsPersistedAttributionLineage(t *testing.T) {
	tracker := newTestTracker(t)
	_, task := tracker.StartTask(context.Background(), "retry-correction")
	retryOf := uuid.New()
	if err := task.MarkRetry("timeout", WithRetryOf(retryOf)); err != nil {
		t.Fatal(err)
	}
	events, _ := tracker.Buffer().QueryEvents(task.Task.TaskID.String())
	if len(events) != 1 {
		t.Fatalf("events = %d", len(events))
	}
	if err := task.MarkNotRetry(events[0].EventID); err != nil {
		t.Fatal(err)
	}
	corrected, _ := tracker.Buffer().QueryEvents(task.Task.TaskID.String())
	if len(corrected) != 1 || corrected[0].IsRetry || corrected[0].RetryOf != nil {
		t.Fatalf("retry correction failed: %+v", corrected)
	}
	if corrected[0].Details["attribution_operation_id"] != corrected[0].EventID.String() {
		t.Fatalf("corrected operation identity = %v", corrected[0].Details)
	}
	if attempt, ok := positiveDetailInt(corrected[0].Details["attribution_attempt_number"]); !ok || attempt != 1 {
		t.Fatalf("corrected attempt identity = %v", corrected[0].Details)
	}
}
