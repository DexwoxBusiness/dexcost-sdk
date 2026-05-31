// Package integrations provides optional hooks for third-party frameworks.
//
// DexcostCallbackHandler is duck-typed to satisfy the
// github.com/tmc/langchaingo/callbacks.Handler interface WITHOUT importing
// langchaingo as a dependency. Users who have langchaingo in their module
// can pass this handler wherever callbacks.Handler is accepted; Go's
// structural typing takes care of the rest.
//
// All recording is wrapped in recover/defer so the handler never panics
// inside a user's LLM call path.
package integrations

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/shopspring/decimal"
)

// pendingRun stores information captured in HandleLLMStart that is needed
// when HandleLLMEnd fires.
type pendingRun struct {
	model   string
	prompts []string
	start   time.Time
}

// DexcostCallbackHandler tracks LLM costs for langchaingo calls.
// It implements the langchaingo callbacks.Handler interface via duck typing
// (structural match) so that no import of langchaingo is required.
type DexcostCallbackHandler struct {
	buffer  core.Buffer
	pricing *pricing.Engine

	mu       sync.Mutex
	pending  map[string]*pendingRun // keyed by goroutine-local context identity
	modelHint string               // optional default model name
}

// NewDexcostCallbackHandler creates a handler that records llm_call events
// to the given buffer with costs computed by the pricing engine.
func NewDexcostCallbackHandler(buffer core.Buffer, engine *pricing.Engine) *DexcostCallbackHandler {
	return &DexcostCallbackHandler{
		buffer:  buffer,
		pricing: engine,
		pending: make(map[string]*pendingRun),
	}
}

// WithModel sets a default model name used when the output does not
// carry model information. Returns the receiver for chaining.
func (h *DexcostCallbackHandler) WithModel(model string) *DexcostCallbackHandler {
	h.modelHint = model
	return h
}

// HandleLLMStart is called when an LLM call begins.
// prompts contains the input strings sent to the model.
func (h *DexcostCallbackHandler) HandleLLMStart(ctx context.Context, prompts []string) {
	defer func() { recover() }() //nolint:errcheck

	key := pendingKeyFromCtx(ctx)
	h.mu.Lock()
	h.pending[key] = &pendingRun{
		model:   h.modelHint,
		prompts: prompts,
		start:   time.Now().UTC(),
	}
	h.mu.Unlock()
}

// HandleLLMEnd is called when an LLM call completes.
// output is expected to be a langchaingo llms.ContentResponse or similar
// struct; we extract token usage via map/struct field access.
func (h *DexcostCallbackHandler) HandleLLMEnd(ctx context.Context, output interface{}) {
	defer func() { recover() }() //nolint:errcheck

	task := core.GetCurrentTask(ctx)
	if task == nil {
		return
	}

	key := pendingKeyFromCtx(ctx)
	h.mu.Lock()
	run, ok := h.pending[key]
	delete(h.pending, key)
	h.mu.Unlock()

	startTime := time.Now().UTC()
	if ok && run != nil {
		startTime = run.start
	}

	model := h.modelHint
	if ok && run != nil && run.model != "" {
		model = run.model
	}

	inputTokens, outputTokens, cachedTokens := extractTokenUsage(output)

	latencyMs := int(time.Since(startTime).Milliseconds())

	costResult := h.pricing.GetCost(model, inputTokens, outputTokens, cachedTokens, 0)

	event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
	event.Provider = "langchaingo"
	event.Model = model
	event.CostUSD = costResult.CostUSD
	event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
	event.PricingSource = core.PricingSource(costResult.PricingSource)
	event.PricingVersion = costResult.PricingVersion
	event.InputTokens = intPtr(inputTokens)
	event.OutputTokens = intPtr(outputTokens)
	if cachedTokens > 0 {
		event.CachedTokens = intPtr(cachedTokens)
	}
	event.LatencyMs = &latencyMs

	if err := h.buffer.InsertEvent(event); err != nil {
		log.Printf("[dexcost] failed to persist event: %v", err)
	}
}

// HandleLLMGenerateContentStart is called when a GenerateContent call begins.
func (h *DexcostCallbackHandler) HandleLLMGenerateContentStart(ctx context.Context, ms []interface{}) {
	defer func() { recover() }() //nolint:errcheck

	key := pendingKeyFromCtx(ctx)
	h.mu.Lock()
	h.pending[key] = &pendingRun{
		model: h.modelHint,
		start: time.Now().UTC(),
	}
	h.mu.Unlock()
}

// HandleLLMGenerateContentEnd is called when a GenerateContent call completes.
// output is expected to carry token usage information.
func (h *DexcostCallbackHandler) HandleLLMGenerateContentEnd(ctx context.Context, output interface{}) {
	defer func() { recover() }() //nolint:errcheck

	// Delegate to the same logic as HandleLLMEnd.
	h.HandleLLMEnd(ctx, output)
}

// --- No-op handlers required by callbacks.Handler ---

// HandleChainStart is a no-op.
func (h *DexcostCallbackHandler) HandleChainStart(ctx context.Context, inputs map[string]interface{}) {
}

// HandleChainEnd is a no-op.
func (h *DexcostCallbackHandler) HandleChainEnd(ctx context.Context, outputs map[string]interface{}) {
}

// HandleChainError is a no-op.
func (h *DexcostCallbackHandler) HandleChainError(ctx context.Context, err error) {
}

// HandleToolStart is a no-op.
func (h *DexcostCallbackHandler) HandleToolStart(ctx context.Context, input string) {
}

// HandleToolEnd is a no-op.
func (h *DexcostCallbackHandler) HandleToolEnd(ctx context.Context, output string) {
}

// HandleToolError is a no-op.
func (h *DexcostCallbackHandler) HandleToolError(ctx context.Context, err error) {
}

// HandleLLMError is a no-op.
func (h *DexcostCallbackHandler) HandleLLMError(ctx context.Context, err error) {
}

// HandleText is a no-op.
func (h *DexcostCallbackHandler) HandleText(ctx context.Context, text string) {
}

// HandleAgentAction is a no-op.
func (h *DexcostCallbackHandler) HandleAgentAction(ctx context.Context, action interface{}) {
}

// HandleAgentFinish is a no-op.
func (h *DexcostCallbackHandler) HandleAgentFinish(ctx context.Context, finish interface{}) {
}

// HandleRetrieverStart is a no-op.
func (h *DexcostCallbackHandler) HandleRetrieverStart(ctx context.Context, query string) {
}

// HandleRetrieverEnd is a no-op.
func (h *DexcostCallbackHandler) HandleRetrieverEnd(ctx context.Context, query string, documents []interface{}) {
}

// HandleStreamingFunc is a no-op.
func (h *DexcostCallbackHandler) HandleStreamingFunc(ctx context.Context, chunk []byte) {
}

// --- Helpers ---

// pendingKeyFromCtx derives a stable key for the pending-run map.
// We use the task ID from context so that concurrent tasks don't collide.
func pendingKeyFromCtx(ctx context.Context) string {
	task := core.GetCurrentTask(ctx)
	if task != nil {
		return task.TaskID.String()
	}
	return "unknown"
}

// extractTokenUsage attempts to pull token counts from a langchaingo output
// using map access (for map[string]interface{}) or struct field matching.
// Returns (inputTokens, outputTokens, cachedTokens).
func extractTokenUsage(output interface{}) (int, int, int) {
	if output == nil {
		return 0, 0, 0
	}

	// Try map[string]interface{} — common when output is serialised.
	if m, ok := output.(map[string]interface{}); ok {
		return intFromMap(m, "PromptTokens", "prompt_tokens", "input_tokens"),
			intFromMap(m, "CompletionTokens", "completion_tokens", "output_tokens"),
			intFromMap(m, "CachedTokens", "cached_tokens")
	}

	// Try to read exported fields via a simple interface assertion pattern.
	// langchaingo ContentResponse has a Choices field; usage may be nested.
	type usageCarrier interface {
		GetUsage() map[string]int
	}
	if uc, ok := output.(usageCarrier); ok {
		u := uc.GetUsage()
		return u["prompt_tokens"], u["completion_tokens"], u["cached_tokens"]
	}

	return 0, 0, 0
}

// intFromMap tries multiple keys and returns the first int-like value found.
func intFromMap(m map[string]interface{}, keys ...string) int {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			switch val := v.(type) {
			case int:
				return val
			case int64:
				return int(val)
			case float64:
				return int(val)
			case decimal.Decimal:
				return int(val.IntPart())
			}
		}
	}
	return 0
}

// intPtr returns a pointer to the given int.
func intPtr(v int) *int {
	return &v
}
