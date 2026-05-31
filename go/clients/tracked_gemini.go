package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/DexwoxBusiness/dexcost-sdk/go/security"
)

// TrackedGemini wraps a Google Gemini-compatible client to automatically record
// LLM cost events for every content generation call. The inner client is
// accepted as interface{} so that the dexcost SDK does not depend on any
// specific Google AI Go SDK.
//
// The wrapper expects the inner client to expose a method with the signature:
//
//	GenerateContent(ctx context.Context, req interface{}) (interface{}, error)
//
// If the inner client does not satisfy this interface, calls will return an error.
//
// Usage with google/generative-ai-go:
//
//	client, _ := genai.NewClient(ctx, option.WithAPIKey(apiKey))
//	model := client.GenerativeModel("gemini-1.5-pro")
//	tracked := clients.NewTrackedGemini(model, tracker, pricingEngine)
//	resp, err := tracked.GenerateContent(ctx, req)
type TrackedGemini struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// geminiContentGenerator is the interface that the inner client must satisfy.
type geminiContentGenerator interface {
	GenerateContent(ctx context.Context, req interface{}) (interface{}, error)
}

// NewTrackedGemini creates a new TrackedGemini wrapper.
func NewTrackedGemini(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedGemini {
	return &TrackedGemini{inner: inner, tracker: tracker, pricing: pricing}
}

// GenerateContent calls the inner client's GenerateContent method, records an
// llm_call event with cost/token data, and returns the response. If no task
// is present in ctx, an auto-task is created and finalized.
func (t *TrackedGemini) GenerateContent(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	// Determine task context.
	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "gemini.generateContent")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "google"
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSourceUnknown
		event.LatencyMs = &latencyMs
		event.Details["error"] = security.ScrubURLsInText(err.Error())
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
		event.Provider = "google"
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

	event, recordErr := RecordGeminiResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		event = core.NewEvent(task.TaskID, core.EventTypeLLMCall)
		event.Provider = "google"
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

// callInner invokes the inner client's GenerateContent method.
func (t *TrackedGemini) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if gc, ok := t.inner.(geminiContentGenerator); ok {
		return gc.GenerateContent(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner Gemini client does not implement GenerateContent(context.Context, interface{}) (interface{}, error)")
}
