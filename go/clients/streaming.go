package clients

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// StreamProvider is the LLM provider behind a streaming response. The wire
// format is the same shape per provider as the unary helpers — see
// RecordOpenAIResponse / RecordAnthropicResponse for the (non-streaming)
// equivalents.
type StreamProvider int

const (
	// StreamProviderOpenAI parses OpenAI-style SSE chunks: `data: {...}\n\n`
	// where each JSON payload is a `chat.completion.chunk` and the final
	// chunk carries `usage.{prompt_tokens,completion_tokens}` when the
	// caller passes `stream_options: {include_usage: true}`.
	StreamProviderOpenAI StreamProvider = iota
	// StreamProviderAnthropic parses Anthropic-style SSE chunks. Anthropic
	// emits two relevant event types: `message_start` (carries `model` +
	// initial `usage.input_tokens`) and `message_delta` (carries cumulative
	// `usage.output_tokens` up to the latest delta).
	StreamProviderAnthropic
	// StreamProviderGroq is OpenAI-compatible; identical wire format,
	// stamped with `provider = "groq"` on the recorded event.
	StreamProviderGroq
)

// StreamRecorder wraps an SSE response body to accumulate token usage from
// streaming chunks and record an llm_call event when the stream is fully
// consumed (or closed by the caller). It implements `io.ReadCloser` so it can
// be substituted in place of the original `*http.Response.Body`.
//
// The wrapper is transparent — `Read` returns bytes byte-for-byte from the
// underlying stream — but tees buffered reads through an SSE chunk parser.
// When the underlying stream returns `io.EOF` (or the wrapper is `Close`d
// before EOF), the accumulated usage is recorded as a single llm_call event
// against the supplied `taskID`. Subsequent `Close` calls are no-ops.
//
// This mirrors the Python streaming pattern at `instruments/openai.py:257`
// (sync) and `:313` (async) — wrap the iterator, watch each chunk for
// `model`+`usage`, finalize on stream end.
type StreamRecorder struct {
	body         io.ReadCloser
	provider     StreamProvider
	buffer       core.Buffer
	pricingEng   *pricing.Engine
	taskID       uuid.UUID
	requestModel string

	startTime time.Time

	scanBuf bytes.Buffer
	model   string
	usage   map[string]int

	mu        sync.Mutex
	finalized bool
}

// NewOpenAIStreamRecorder wraps an OpenAI streaming chat completion response.
// `requestModel` is used as a fallback when chunks don't carry a `model`
// field. Pass the body verbatim from `*http.Response.Body`; the recorder
// implements `io.ReadCloser` and forwards reads transparently.
func NewOpenAIStreamRecorder(
	body io.ReadCloser,
	bufferStore core.Buffer,
	pricingEng *pricing.Engine,
	taskID uuid.UUID,
	requestModel string,
) *StreamRecorder {
	return newStreamRecorder(body, StreamProviderOpenAI, bufferStore, pricingEng, taskID, requestModel)
}

// NewAnthropicStreamRecorder wraps an Anthropic streaming messages response.
func NewAnthropicStreamRecorder(
	body io.ReadCloser,
	bufferStore core.Buffer,
	pricingEng *pricing.Engine,
	taskID uuid.UUID,
	requestModel string,
) *StreamRecorder {
	return newStreamRecorder(body, StreamProviderAnthropic, bufferStore, pricingEng, taskID, requestModel)
}

// NewGroqStreamRecorder wraps a Groq streaming chat completion response.
// Groq is wire-compatible with OpenAI; this constructor exists so the
// recorded event carries `provider = "groq"`.
func NewGroqStreamRecorder(
	body io.ReadCloser,
	bufferStore core.Buffer,
	pricingEng *pricing.Engine,
	taskID uuid.UUID,
	requestModel string,
) *StreamRecorder {
	return newStreamRecorder(body, StreamProviderGroq, bufferStore, pricingEng, taskID, requestModel)
}

func newStreamRecorder(
	body io.ReadCloser,
	provider StreamProvider,
	bufferStore core.Buffer,
	pricingEng *pricing.Engine,
	taskID uuid.UUID,
	requestModel string,
) *StreamRecorder {
	return &StreamRecorder{
		body:         body,
		provider:     provider,
		buffer:       bufferStore,
		pricingEng:   pricingEng,
		taskID:       taskID,
		requestModel: requestModel,
		startTime:    time.Now(),
		usage:        make(map[string]int),
	}
}

// Read forwards bytes from the underlying body and tees them through the
// SSE chunk parser. On any terminal error (EOF or otherwise — e.g. the
// server hangs up mid-stream with io.ErrUnexpectedEOF) the accumulated
// usage is recorded so callers that drain via io.ReadAll without an
// explicit Close don't silently lose the event.
func (s *StreamRecorder) Read(p []byte) (int, error) {
	n, err := s.body.Read(p)
	if n > 0 {
		s.scanBuf.Write(p[:n])
		s.consumeBuffered()
	}
	if err != nil {
		s.finalize()
	}
	return n, err
}

// Close ends the stream early and finalizes the event. Safe to call multiple
// times — subsequent calls are no-ops.
func (s *StreamRecorder) Close() error {
	s.finalize()
	return s.body.Close()
}

// Usage returns the accumulated input/output/cached token counts. Useful for
// tests and for callers who want to inspect the recorder after consumption.
func (s *StreamRecorder) Usage() (input, output, cached int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.usage["input"], s.usage["output"], s.usage["cached"]
}

// Model returns the model name observed during streaming. If the stream
// never emitted a model field, the fallback `requestModel` is returned.
func (s *StreamRecorder) Model() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.model != "" {
		return s.model
	}
	return s.requestModel
}

// consumeBuffered scans the internal buffer for complete SSE events
// (terminated by a blank line) and feeds each one through the chunk parser.
// Partial events at the tail are kept in the buffer for the next call.
func (s *StreamRecorder) consumeBuffered() {
	data := s.scanBuf.Bytes()
	for {
		idx := bytes.Index(data, []byte("\n\n"))
		if idx < 0 {
			break
		}
		event := data[:idx]
		data = data[idx+2:]
		s.processSSEEvent(event)
	}
	// Re-buffer the unconsumed tail.
	s.scanBuf.Reset()
	s.scanBuf.Write(data)
}

// processSSEEvent extracts the JSON `data:` lines from an SSE event block
// and dispatches them to the provider-specific chunk processor.
func (s *StreamRecorder) processSSEEvent(event []byte) {
	scanner := bufio.NewScanner(bytes.NewReader(event))
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "" || payload == "[DONE]" {
			continue
		}
		var chunk map[string]interface{}
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			continue
		}
		switch s.provider {
		case StreamProviderOpenAI, StreamProviderGroq:
			s.processOpenAIChunk(chunk)
		case StreamProviderAnthropic:
			s.processAnthropicChunk(chunk)
		}
	}
}

// processOpenAIChunk pulls model + usage from an OpenAI-shaped chunk.
// Usage typically appears only on the final chunk when the caller passes
// `stream_options: {include_usage: true}`.
func (s *StreamRecorder) processOpenAIChunk(chunk map[string]interface{}) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if m, ok := chunk["model"].(string); ok && m != "" {
		s.model = m
	}
	if usage, ok := chunk["usage"].(map[string]interface{}); ok {
		if v := intFromMap(usage, "prompt_tokens"); v > 0 {
			s.usage["input"] = v
		}
		if v := intFromMap(usage, "completion_tokens"); v > 0 {
			s.usage["output"] = v
		}
		if v := intFromMap(usage, "cached_tokens"); v > 0 {
			s.usage["cached"] = v
		}
	}
}

// processAnthropicChunk handles Anthropic's typed event stream. The
// `message_start` event carries `message.model` and the initial
// `message.usage.input_tokens`; subsequent `message_delta` events emit
// cumulative `usage.output_tokens` up to the latest delta.
func (s *StreamRecorder) processAnthropicChunk(chunk map[string]interface{}) {
	s.mu.Lock()
	defer s.mu.Unlock()
	eventType, _ := chunk["type"].(string)
	switch eventType {
	case "message_start":
		message, ok := chunk["message"].(map[string]interface{})
		if !ok {
			return
		}
		if m, ok := message["model"].(string); ok && m != "" {
			s.model = m
		}
		if usage, ok := message["usage"].(map[string]interface{}); ok {
			if v := intFromMap(usage, "input_tokens"); v > 0 {
				s.usage["input"] = v
			}
			if v := intFromMap(usage, "cache_read_input_tokens"); v > 0 {
				s.usage["cached"] = v
			}
		}
	case "message_delta":
		if usage, ok := chunk["usage"].(map[string]interface{}); ok {
			if v := intFromMap(usage, "output_tokens"); v > 0 {
				s.usage["output"] = v
			}
			if v := intFromMap(usage, "input_tokens"); v > 0 && s.usage["input"] == 0 {
				s.usage["input"] = v
			}
		}
	}
}

// finalize records the accumulated usage as a single llm_call event. Idempotent.
func (s *StreamRecorder) finalize() {
	s.mu.Lock()
	if s.finalized {
		s.mu.Unlock()
		return
	}
	s.finalized = true
	model := s.model
	if model == "" {
		model = s.requestModel
	}
	input := s.usage["input"]
	output := s.usage["output"]
	cached := s.usage["cached"]
	s.mu.Unlock()

	latencyMs := int(time.Since(s.startTime).Milliseconds())

	event := core.NewEvent(s.taskID, core.EventTypeLLMCall)
	event.Provider = s.providerName()
	event.Model = model
	event.LatencyMs = &latencyMs

	if input == 0 && output == 0 {
		event.CostUSD = decimal.Zero
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSourceUnknown
		event.Details["streaming"] = true
	} else {
		costResult := s.pricingEng.GetCost(model, input, output, cached, 0)
		event.CostUSD = costResult.CostUSD
		event.CostConfidence = core.CostConfidence(costResult.CostConfidence)
		event.PricingSource = core.PricingSource(costResult.PricingSource)
		event.PricingVersion = costResult.PricingVersion
		event.InputTokens = intPtr(input)
		event.OutputTokens = intPtr(output)
		if cached > 0 {
			event.CachedTokens = intPtr(cached)
		}
		event.Details["streaming"] = true
	}

	if err := s.buffer.InsertEvent(event); err != nil {
		// finalize runs in the read path, so surfacing this error would force
		// callers to swallow it. Mirror the Python helper's debug-log behavior.
		log.Printf("[dexcost] streaming finalize: insert event failed: %v", err)
	}
}

func (s *StreamRecorder) providerName() string {
	switch s.provider {
	case StreamProviderOpenAI:
		return "openai"
	case StreamProviderAnthropic:
		return "anthropic"
	case StreamProviderGroq:
		return "groq"
	}
	return "unknown"
}
