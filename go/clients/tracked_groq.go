package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
)

// TrackedGroq wraps a Groq chat-completion client to automatically record
// LLM cost events. Groq's wire format is OpenAI-compatible, so the inner
// client should expose the same `CreateChatCompletion` method as the OpenAI
// SDK; the wrapper differentiates Groq usage by stamping `provider = "groq"`
// on the recorded event.
//
//	CreateChatCompletion(ctx context.Context, req interface{}) (interface{}, error)
type TrackedGroq struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// NewTrackedGroq creates a new TrackedGroq wrapper.
func NewTrackedGroq(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedGroq {
	return &TrackedGroq{inner: inner, tracker: tracker, pricing: pricing}
}

// CreateChatCompletion calls the inner client's CreateChatCompletion method,
// records an llm_call event with cost/token data, and returns the response.
// If no task is present in ctx, an auto-task is created and finalized.
func (t *TrackedGroq) CreateChatCompletion(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "groq.chat")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		event := minimalFailedEvent(task.TaskID, "groq", latencyMs, err)
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
		event := minimalFailedEvent(task.TaskID, "groq", latencyMs, nil)
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}
		logEvent(&event, task.TaskType)
		if autoCreated {
			core.FinalizeAutoTask(task, &event, string(core.TaskStatusSuccess), t.tracker.Buffer())
		}
		return resp, nil
	}

	event, recordErr := RecordGroqResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		event = minimalFailedEvent(task.TaskID, "groq", latencyMs, nil)
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

// callInner invokes the inner client's CreateChatCompletion method.
func (t *TrackedGroq) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if cc, ok := t.inner.(openaiCompletionCreator); ok {
		return cc.CreateChatCompletion(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner Groq client does not implement CreateChatCompletion(context.Context, interface{}) (interface{}, error)")
}
