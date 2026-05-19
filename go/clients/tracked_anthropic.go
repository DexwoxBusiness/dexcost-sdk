package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// TrackedAnthropic wraps an Anthropic-compatible client to automatically record
// LLM cost events for every message creation call. The inner client is accepted
// as interface{} so that the dexcost SDK does not depend on any specific
// Anthropic Go SDK.
//
// The wrapper expects the inner client to expose a method with the signature:
//
//	CreateMessage(ctx context.Context, req interface{}) (interface{}, error)
//
// If the inner client does not satisfy this interface, calls will return an error.
//
// Usage with anthropics/anthropic-sdk-go:
//
//	client := anthropic.NewClient(apiKey)
//	tracked := clients.NewTrackedAnthropic(client, tracker, pricingEngine)
//	resp, err := tracked.CreateMessage(ctx, req)
type TrackedAnthropic struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// anthropicMessageCreator is the interface that the inner client must satisfy.
type anthropicMessageCreator interface {
	CreateMessage(ctx context.Context, req interface{}) (interface{}, error)
}

// NewTrackedAnthropic creates a new TrackedAnthropic wrapper.
func NewTrackedAnthropic(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedAnthropic {
	return &TrackedAnthropic{inner: inner, tracker: tracker, pricing: pricing}
}

// CreateMessage calls the inner client's CreateMessage method, records an
// llm_call event with cost/token data, and returns the response. If no
// task is present in ctx, an auto-task is created and finalized.
func (t *TrackedAnthropic) CreateMessage(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	// Determine task context.
	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "anthropic.messages")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "anthropic"
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSourceUnknown
		event.LatencyMs = &latencyMs
		event.Details["error"] = err.Error()
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}

		logEvent(&event, task.TaskType)

		if autoCreated {
			core.FinalizeAutoTask(task, &event, string(core.TaskStatusFailed), t.tracker.Buffer())
		}
		return nil, err
	}

	respMap, mapErr := toResponseMap(resp)
	if mapErr != nil {
		event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "anthropic"
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSourceUnknown
		event.LatencyMs = &latencyMs
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}

		logEvent(&event, task.TaskType)

		if autoCreated {
			core.FinalizeAutoTask(task, &event, string(core.TaskStatusSuccess), t.tracker.Buffer())
		}
		return resp, nil
	}

	event, recordErr := RecordAnthropicResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		event = core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "anthropic"
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSourceUnknown
		event.LatencyMs = &latencyMs
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}
	} else {
		event.LatencyMs = &latencyMs
		if updErr := t.tracker.Buffer().UpdateEvent(event); updErr != nil {
			log.Printf("[dexcost] failed to update event: %v", updErr)
		}
	}

	logEvent(&event, task.TaskType)

	if autoCreated {
		core.FinalizeAutoTask(task, &event, string(core.TaskStatusSuccess), t.tracker.Buffer())
	}

	return resp, nil
}

// callInner invokes the inner client's CreateMessage method.
func (t *TrackedAnthropic) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if mc, ok := t.inner.(anthropicMessageCreator); ok {
		return mc.CreateMessage(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner Anthropic client does not implement CreateMessage(context.Context, interface{}) (interface{}, error)")
}
