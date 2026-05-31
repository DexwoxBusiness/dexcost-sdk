package clients

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/DexwoxBusiness/dexcost-sdk/go/security"
	"github.com/google/uuid"
)

// TrackedBedrock wraps an AWS Bedrock-runtime client to automatically record
// LLM cost events for every InvokeModel call. The inner client is accepted
// as interface{} so that the dexcost SDK does not depend on the AWS SDK for Go.
//
// The wrapper expects the inner client to expose a method with the signature:
//
//	InvokeModel(ctx context.Context, req interface{}) (interface{}, error)
//
// The returned response is expected to be a map[string]interface{} with
// shape:
//
//	{
//	  "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
//	  "body":    map[string]interface{}{ ... family-specific shape ... },
//	}
//
// Token extraction handles the differing body schemas across model families
// (Anthropic, Amazon Titan, Meta Llama, Cohere, AI21, Mistral) — see
// RecordBedrockResponse.
type TrackedBedrock struct {
	inner   interface{}
	tracker *core.Tracker
	pricing *pricing.Engine
}

// bedrockInvokeModelClient is the interface that the inner client must satisfy.
type bedrockInvokeModelClient interface {
	InvokeModel(ctx context.Context, req interface{}) (interface{}, error)
}

// NewTrackedBedrock creates a new TrackedBedrock wrapper.
func NewTrackedBedrock(inner interface{}, tracker *core.Tracker, pricing *pricing.Engine) *TrackedBedrock {
	return &TrackedBedrock{inner: inner, tracker: tracker, pricing: pricing}
}

// InvokeModel calls the inner client's InvokeModel method, records an
// llm_call event with cost/token data, and returns the response. If no
// task is present in ctx, an auto-task is created and finalized.
func (t *TrackedBedrock) InvokeModel(ctx context.Context, req interface{}) (interface{}, error) {
	start := time.Now()

	resp, err := t.callInner(ctx, req)

	latencyMs := int(time.Since(start).Milliseconds())

	autoCreated := false
	var task *core.Task
	if existing := core.GetCurrentTask(ctx); existing != nil {
		task = existing
	} else {
		auto := core.CreateAutoTask(ctx, "bedrock.invoke")
		task = &auto
		autoCreated = true
		if insErr := t.tracker.Buffer().InsertTask(*task); insErr != nil {
			log.Printf("[dexcost] failed to persist task: %v", insErr)
		}
	}

	if err != nil {
		event := minimalFailedEvent(task.TaskID, "aws_bedrock", latencyMs, err)
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
		event := minimalFailedEvent(task.TaskID, "aws_bedrock", latencyMs, nil)
		if insErr := t.tracker.Buffer().InsertEvent(event); insErr != nil {
			log.Printf("[dexcost] failed to persist event: %v", insErr)
		}
		logEvent(&event, task.TaskType)
		if autoCreated {
			core.FinalizeAutoTask(task, &event, string(core.TaskStatusSuccess), t.tracker.Buffer())
		}
		return resp, nil
	}

	event, recordErr := RecordBedrockResponse(t.tracker.Buffer(), t.pricing, task.TaskID, respMap)
	if recordErr != nil {
		event = minimalFailedEvent(task.TaskID, "aws_bedrock", latencyMs, nil)
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

// callInner invokes the inner client's InvokeModel method.
func (t *TrackedBedrock) callInner(ctx context.Context, req interface{}) (interface{}, error) {
	if im, ok := t.inner.(bedrockInvokeModelClient); ok {
		return im.InvokeModel(ctx, req)
	}
	return nil, fmt.Errorf("clients: inner Bedrock client does not implement InvokeModel(context.Context, interface{}) (interface{}, error)")
}

// minimalFailedEvent builds a baseline llm_call event with provider/latency
// populated and confidence set to "unknown". Used by tracked wrappers when
// the call errored or the response shape was unparseable.
func minimalFailedEvent(taskID uuid.UUID, provider string, latencyMs int, err error) core.Event {
	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = provider
	event.CostConfidence = core.CostConfidenceUnknown
	event.PricingSource = core.PricingSourceUnknown
	event.LatencyMs = &latencyMs
	if err != nil {
		event.Details["error"] = security.ScrubURLsInText(err.Error())
	}
	return event
}
