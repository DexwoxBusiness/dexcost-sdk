package core

import (
	"fmt"
	"sync"

	"github.com/google/uuid"
)

// TransientErrors is the set of error types that indicate a transient failure
// likely to be retried.
var TransientErrors = map[string]bool{
	"rate_limit":       true,
	"timeout":          true,
	"5xx":              true,
	"server_error":     true,
	"connection_error": true,
}

// ErrorLikelihoods maps each transient error type to a base likelihood that
// a subsequent identical LLM call is a retry.
var ErrorLikelihoods = map[string]float64{
	"rate_limit":       1.0,
	"timeout":          0.9,
	"5xx":              0.85,
	"server_error":     0.85,
	"connection_error": 0.8,
}

// HeuristicMatch holds the result of a retry heuristic check.
type HeuristicMatch struct {
	IsRetry        bool
	Confidence     float64
	MatchedEventID *uuid.UUID
	Reason         string
}

// RetryHeuristicEngine detects likely retries by inspecting recent events for
// the same task and model within a rolling time window.
type RetryHeuristicEngine struct {
	mu            sync.Mutex
	windowSeconds float64
	threshold     float64
	recentEvents  map[uuid.UUID][]Event // taskID -> events
}

// NewRetryHeuristicEngine creates a new engine with the given window (seconds)
// and confidence threshold.
func NewRetryHeuristicEngine(windowSeconds, threshold float64) *RetryHeuristicEngine {
	if windowSeconds <= 0 {
		panic(fmt.Sprintf("dexcost: windowSeconds must be positive, got %f", windowSeconds))
	}
	if threshold <= 0 || threshold > 1 {
		panic(fmt.Sprintf("dexcost: threshold must be in (0, 1], got %f", threshold))
	}
	return &RetryHeuristicEngine{
		windowSeconds: windowSeconds,
		threshold:     threshold,
		recentEvents:  make(map[uuid.UUID][]Event),
	}
}

// WindowSeconds returns the rolling window size in seconds.
func (e *RetryHeuristicEngine) WindowSeconds() float64 {
	return e.windowSeconds
}

// Threshold returns the minimum confidence required to flag a retry.
func (e *RetryHeuristicEngine) Threshold() float64 {
	return e.threshold
}

// Record stores the event for future Check calls, pruning events that fall
// outside the rolling window relative to the event's OccurredAt time.
func (e *RetryHeuristicEngine) Record(event Event) {
	e.mu.Lock()
	defer e.mu.Unlock()

	// Prune events older than windowSeconds relative to this event's time.
	existing := e.recentEvents[event.TaskID]
	pruned := existing[:0]
	for _, candidate := range existing {
		gap := event.OccurredAt.Sub(candidate.OccurredAt).Seconds()
		if gap >= 0 && gap <= e.windowSeconds {
			pruned = append(pruned, candidate)
		}
	}

	if len(pruned) == 0 {
		// All previous events expired; start fresh
		e.recentEvents[event.TaskID] = []Event{event}
	} else {
		e.recentEvents[event.TaskID] = append(pruned, event)
	}
}

// Check inspects recent events for the same task and model to determine whether
// the supplied event is likely a retry. It walks backwards through recorded
// events and returns a HeuristicMatch.
//
// Algorithm (matches Python/TypeScript implementations):
//  1. Walk backwards through events for event.TaskID.
//  2. Skip if same EventID, not an llm_call, or different Model.
//  3. On first same-model candidate, check Details["error_type"].
//  4. If no error_type or not transient: return no-match.
//  5. Compute gap; if out of range: return no-match.
//  6. confidence = baseLikelihood * max(0, 1 - gap/windowSeconds).
//  7. If confidence >= threshold: return match; else: return no-match.
func (e *RetryHeuristicEngine) Check(event Event) HeuristicMatch {
	e.mu.Lock()
	defer e.mu.Unlock()

	noMatch := HeuristicMatch{}

	events, ok := e.recentEvents[event.TaskID]
	if !ok || len(events) == 0 {
		return noMatch
	}

	// Walk backwards.
	for i := len(events) - 1; i >= 0; i-- {
		candidate := events[i]

		// Skip self.
		if candidate.EventID == event.EventID {
			continue
		}
		// Only consider LLM calls.
		if candidate.EventType != EventTypeLLMCall {
			continue
		}
		// Must be the same model.
		if candidate.Model != event.Model {
			continue
		}

		// Found a same-model LLM call — inspect error_type.
		errTypeRaw, hasErr := candidate.Details["error_type"]
		if !hasErr || errTypeRaw == nil {
			return noMatch
		}
		errorType, ok := errTypeRaw.(string)
		if !ok || !TransientErrors[errorType] {
			return noMatch
		}

		// Compute time gap.
		gap := event.OccurredAt.Sub(candidate.OccurredAt).Seconds()
		if gap < 0 || gap > e.windowSeconds {
			return noMatch
		}

		// Base likelihood with default fallback.
		baseLikelihood, exists := ErrorLikelihoods[errorType]
		if !exists {
			baseLikelihood = 0.8
		}

		// Time decay.
		timeDecay := 1.0 - gap/e.windowSeconds
		if timeDecay < 0 {
			timeDecay = 0
		}
		confidence := baseLikelihood * timeDecay

		if confidence >= e.threshold {
			matchedID := candidate.EventID
			return HeuristicMatch{
				IsRetry:        true,
				Confidence:     confidence,
				MatchedEventID: &matchedID,
				Reason:         errorType,
			}
		}
		return noMatch
	}

	return noMatch
}
