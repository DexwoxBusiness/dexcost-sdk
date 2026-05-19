package core

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

// helper: make an LLM call event for the given task, model, and time offset.
func makeLLMEvent(taskID uuid.UUID, model string, occurredAt time.Time) Event {
	e := NewEvent(taskID, EventTypeLLMCall)
	e.Model = model
	e.OccurredAt = occurredAt
	return e
}

// helper: set error_type in Details and return the event.
func withErrorType(e Event, errType string) Event {
	e.Details["error_type"] = errType
	return e
}

// TestHeuristic_DetectsRetryAfterTransientError verifies that a second LLM call
// on the same task + model within the window, after a transient error, is flagged.
func TestHeuristic_DetectsRetryAfterTransientError(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	taskID := uuid.New()
	now := time.Now().UTC()

	// First call: failed with rate_limit 5 seconds ago.
	first := makeLLMEvent(taskID, "gpt-4", now.Add(-5*time.Second))
	first = withErrorType(first, "rate_limit")
	engine.Record(first)

	// Second call: retry candidate now.
	second := makeLLMEvent(taskID, "gpt-4", now)

	match := engine.Check(second)
	if !match.IsRetry {
		t.Error("expected IsRetry=true after transient error within window")
	}
	if match.MatchedEventID == nil {
		t.Error("expected non-nil MatchedEventID")
	}
	if *match.MatchedEventID != first.EventID {
		t.Errorf("expected MatchedEventID=%s, got %s", first.EventID, *match.MatchedEventID)
	}
	if match.Confidence <= 0 {
		t.Error("expected positive Confidence")
	}
}

// TestHeuristic_NoFlagDifferentModel verifies no retry detection when model differs.
func TestHeuristic_NoFlagDifferentModel(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	taskID := uuid.New()
	now := time.Now().UTC()

	first := makeLLMEvent(taskID, "gpt-4", now.Add(-5*time.Second))
	first = withErrorType(first, "rate_limit")
	engine.Record(first)

	second := makeLLMEvent(taskID, "gpt-3.5-turbo", now)

	match := engine.Check(second)
	if match.IsRetry {
		t.Error("expected IsRetry=false for different model")
	}
}

// TestHeuristic_NoFlagDifferentTask verifies no retry detection when task differs.
func TestHeuristic_NoFlagDifferentTask(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	now := time.Now().UTC()
	taskA := uuid.New()
	taskB := uuid.New()

	first := makeLLMEvent(taskA, "gpt-4", now.Add(-5*time.Second))
	first = withErrorType(first, "rate_limit")
	engine.Record(first)

	second := makeLLMEvent(taskB, "gpt-4", now)

	match := engine.Check(second)
	if match.IsRetry {
		t.Error("expected IsRetry=false for different task")
	}
}

// TestHeuristic_NoFlagSuccessfulPreviousCall verifies no retry when previous call had no error.
func TestHeuristic_NoFlagSuccessfulPreviousCall(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	taskID := uuid.New()
	now := time.Now().UTC()

	// First call: succeeded (no error_type).
	first := makeLLMEvent(taskID, "gpt-4", now.Add(-5*time.Second))
	engine.Record(first)

	second := makeLLMEvent(taskID, "gpt-4", now)

	match := engine.Check(second)
	if match.IsRetry {
		t.Error("expected IsRetry=false when previous call had no error_type")
	}
}

// TestHeuristic_NoFlagOutsideWindow verifies no retry when gap exceeds windowSeconds.
func TestHeuristic_NoFlagOutsideWindow(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	taskID := uuid.New()
	now := time.Now().UTC()

	// First call: 60 seconds ago — outside the 30s window.
	first := makeLLMEvent(taskID, "gpt-4", now.Add(-60*time.Second))
	first = withErrorType(first, "rate_limit")
	engine.Record(first)

	second := makeLLMEvent(taskID, "gpt-4", now)

	match := engine.Check(second)
	if match.IsRetry {
		t.Error("expected IsRetry=false when gap exceeds windowSeconds")
	}
}

// TestHeuristic_ConfidenceDecaysWithTime verifies that confidence is lower for larger gaps.
func TestHeuristic_ConfidenceDecaysWithTime(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.1) // low threshold so both pass

	taskID := uuid.New()
	now := time.Now().UTC()

	// Near retry (1 second gap).
	near := makeLLMEvent(taskID, "claude-3", now.Add(-1*time.Second))
	near = withErrorType(near, "rate_limit")
	engine.Record(near)
	nearCandidate := makeLLMEvent(taskID, "claude-3", now)
	nearMatch := engine.Check(nearCandidate)

	// Start fresh engine and use a far retry (25 second gap).
	engine2 := NewRetryHeuristicEngine(30, 0.1)
	taskID2 := uuid.New()
	far := makeLLMEvent(taskID2, "claude-3", now.Add(-25*time.Second))
	far = withErrorType(far, "rate_limit")
	engine2.Record(far)
	farCandidate := makeLLMEvent(taskID2, "claude-3", now)
	farMatch := engine2.Check(farCandidate)

	if !nearMatch.IsRetry {
		t.Error("expected near retry to be detected")
	}
	if !farMatch.IsRetry {
		t.Error("expected far retry to be detected (within window)")
	}
	if nearMatch.Confidence <= farMatch.Confidence {
		t.Errorf("expected near confidence (%f) > far confidence (%f)", nearMatch.Confidence, farMatch.Confidence)
	}
}

// TestHeuristic_PrunesOldEventsFromWindow verifies that Record prunes stale events.
func TestHeuristic_PrunesOldEventsFromWindow(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.5)

	taskID := uuid.New()
	now := time.Now().UTC()

	// Record a stale event (70 seconds ago).
	stale := makeLLMEvent(taskID, "gpt-4", now.Add(-70*time.Second))
	stale = withErrorType(stale, "rate_limit")
	engine.Record(stale)

	// Record a fresh event (5 seconds ago) that does not have an error — acts as anchor.
	fresh := makeLLMEvent(taskID, "gpt-4", now.Add(-5*time.Second))
	engine.Record(fresh)

	// Verify stale was pruned by checking internal state size.
	engine.mu.Lock()
	events := engine.recentEvents[taskID]
	engine.mu.Unlock()

	// The stale event should have been pruned when 'fresh' was recorded.
	// fresh.OccurredAt is 5s ago; stale is 70s ago; window=30s.
	// So only the fresh event should remain.
	if len(events) != 1 {
		t.Errorf("expected 1 event after pruning, got %d", len(events))
	}
}

// TestHeuristic_DefaultWindowAndThreshold verifies default constructor values.
func TestHeuristic_DefaultWindowAndThreshold(t *testing.T) {
	engine := NewRetryHeuristicEngine(30, 0.8)

	if engine.WindowSeconds() != 30 {
		t.Errorf("expected WindowSeconds=30, got %f", engine.WindowSeconds())
	}
	if engine.Threshold() != 0.8 {
		t.Errorf("expected Threshold=0.8, got %f", engine.Threshold())
	}
}
