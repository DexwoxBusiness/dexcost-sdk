package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// TrackedOpenAI wraps an OpenAI-compatible client to automatically record
// LLM cost events for every chat completion call. The inner client is
// accepted as interface{} so that the dexcost SDK does not depend on any
// specific OpenAI Go SDK.
//
// The wrapper expects the inner client to expose a method with the signature:
//
//	CreateChatCompletion(ctx context.Context, req interface{}) (interface{}, error)
//
// If the inner client does not satisfy this interface, calls will return an error.
//
// Usage with sashabaranov/go-openai:
//
//	client := openai.NewClient(apiKey)
//	tracked := clients.NewTrackedOpenAI(client, tracker, pricingEngine)
//	resp, err := tracked.CreateChatCompletion(ctx, req)
type TrackedOpenAI struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// openaiCompletionCreator is the interface that the inner client must satisfy.
type openaiCompletionCreator interface {
	CreateChatCompletion(ctx context.Context, req interface{}) (interface{}, error)
}

// NewTrackedOpenAI creates a new TrackedOpenAI wrapper.
func NewTrackedOpenAI(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedOpenAI {
	return &TrackedOpenAI{inner: inner, tracker: tracker, pricing: pricing}
}

// CreateChatCompletion calls the inner client's CreateChatCompletion method,
// records an llm_call event with cost/token data, and returns the response.
// If no task is present in ctx, an auto-task is created and finalized.
func (t *TrackedOpenAI) CreateChatCompletion(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	// Call the inner client.
	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	// Determine task context.
	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "openai.chat")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		// Record a failed event with zero cost.
		event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "openai"
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

	// Extract response fields via type assertion to map.
	respMap, mapErr := toResponseMap(resp)
	if mapErr != nil {
		// Response is not a map; record a minimal event.
		event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "openai"
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

	event, recordErr := RecordOpenAIResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		// Fallback: record a minimal event.
		event = core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "openai"
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

// callInner invokes the inner client's CreateChatCompletion method.
func (t *TrackedOpenAI) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if cc, ok := t.inner.(openaiCompletionCreator); ok {
		return cc.CreateChatCompletion(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner OpenAI client does not implement CreateChatCompletion(context.Context, interface{}) (interface{}, error)")
}

// logEvent is a package-level helper that calls the dev console LogEvent
// if the devLogFunc has been set via SetDevLogFunc.
func logEvent(event *core.Event, taskType string) {
	if devLogFunc != nil {
		devLogFunc(event, taskType)
	}
}

// DevLogFunc is the signature for the dev console log callback.
type DevLogFunc func(event *core.Event, taskType string)

var devLogFunc DevLogFunc

// SetDevLogFunc sets the function used by tracked clients to log events
// to the dev console. This is called by the top-level dexcost package
// to wire up LogEvent without creating an import cycle.
func SetDevLogFunc(f DevLogFunc) {
	devLogFunc = f
}

// toResponseMap attempts to convert a response to map[string]interface{}.
func toResponseMap(resp interface{}) (map[string]interface{}, error) {
	if resp == nil {
		return nil, fmt.Errorf("nil response")
	}
	if m, ok := resp.(map[string]interface{}); ok {
		return m, nil
	}
	return nil, fmt.Errorf("response is not map[string]interface{}")
}
