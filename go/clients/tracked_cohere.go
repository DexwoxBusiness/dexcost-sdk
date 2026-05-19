package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// TrackedCohere wraps a Cohere chat-completion client to automatically record
// LLM cost events. The inner client is accepted as interface{} so that the
// dexcost SDK does not depend on any specific Cohere Go SDK.
//
// The wrapper expects the inner client to expose:
//
//	Chat(ctx context.Context, req interface{}) (interface{}, error)
//
// Token counts are extracted from the response's `meta.billed_units`
// sub-map (see RecordCohereResponse).
type TrackedCohere struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// cohereChatCreator is the interface that the inner client must satisfy.
type cohereChatCreator interface {
	Chat(ctx context.Context, req interface{}) (interface{}, error)
}

// NewTrackedCohere creates a new TrackedCohere wrapper.
func NewTrackedCohere(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedCohere {
	return &TrackedCohere{inner: inner, tracker: tracker, pricing: pricing}
}

// Chat calls the inner client's Chat method, records an llm_call event with
// cost/token data, and returns the response. If no task is present in ctx,
// an auto-task is created and finalized.
func (t *TrackedCohere) Chat(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "cohere.chat")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		event := minimalFailedEvent(task.TaskID, "cohere", latencyMs, err)
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
		event := minimalFailedEvent(task.TaskID, "cohere", latencyMs, nil)
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}
		logEvent(&event, task.TaskType)
		if autoCreated {
			core.FinalizeAutoTask(task, &event, string(core.TaskStatusSuccess), t.tracker.Buffer())
		}
		return resp, nil
	}

	event, recordErr := RecordCohereResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		event = minimalFailedEvent(task.TaskID, "cohere", latencyMs, nil)
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

// callInner invokes the inner client's Chat method.
func (t *TrackedCohere) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if cc, ok := t.inner.(cohereChatCreator); ok {
		return cc.Chat(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner Cohere client does not implement Chat(context.Context, interface{}) (interface{}, error)")
}
