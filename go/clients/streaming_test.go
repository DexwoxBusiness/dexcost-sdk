package clients_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/clients"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// fakeBody is an io.ReadCloser backed by a strings.Reader so we can drive
// `StreamRecorder.Read` deterministically with a known SSE payload.
type fakeBody struct {
	*strings.Reader
	closed bool
	closeErr error
}

func newFakeBody(s string) *fakeBody {
	return &fakeBody{Reader: strings.NewReader(s)}
}

func (b *fakeBody) Close() error {
	b.closed = true
	return b.closeErr
}

// drain reads the entire wrapped body, mirroring how a real consumer would
// process the SSE stream end-to-end.
func drain(t *testing.T, r io.Reader) []byte {
	t.Helper()
	buf, err := io.ReadAll(r)
	if err != nil {
		t.Fatalf("drain stream: %v", err)
	}
	return buf
}

// TestStream_OpenAI_RecordsUsageAndCost — final chunk carries
// `usage.{prompt_tokens,completion_tokens}` per `stream_options:{include_usage:true}`.
func TestStream_OpenAI_RecordsUsageAndCost(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := strings.Join([]string{
		`data: {"id":"a","model":"gpt-4o","choices":[{"delta":{"content":"hi"}}]}`,
		``,
		`data: {"id":"a","model":"gpt-4o","choices":[{"delta":{"content":" there"}}]}`,
		``,
		`data: {"id":"a","model":"gpt-4o","usage":{"prompt_tokens":10,"completion_tokens":7}}`,
		``,
		`data: [DONE]`,
		``,
		``,
	}, "\n")

	body := newFakeBody(sse)
	rec := clients.NewOpenAIStreamRecorder(body, buf, engine, taskID, "gpt-4o")

	out := drain(t, rec)
	if !bytes.Equal(out, []byte(sse)) {
		t.Fatalf("transparent passthrough broken — got %d bytes, want %d", len(out), len(sse))
	}
	in, outTokens, _ := rec.Usage()
	if in != 10 || outTokens != 7 {
		t.Fatalf("usage: got %d/%d want 10/7", in, outTokens)
	}

	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.Provider != "openai" {
		t.Errorf("provider: got %s want openai", ev.Provider)
	}
	if ev.Model != "gpt-4o" {
		t.Errorf("model: got %s want gpt-4o", ev.Model)
	}
	if ev.InputTokens == nil || *ev.InputTokens != 10 {
		t.Errorf("input_tokens: got %v want 10", ev.InputTokens)
	}
	if ev.OutputTokens == nil || *ev.OutputTokens != 7 {
		t.Errorf("output_tokens: got %v want 7", ev.OutputTokens)
	}
	if ev.CostUSD.IsZero() {
		t.Errorf("expected non-zero cost when usage was reported")
	}
	if streaming, _ := ev.Details["streaming"].(bool); !streaming {
		t.Errorf("expected details.streaming=true")
	}
}

// TestStream_OpenAI_NoUsageRecordsZeroCost — when the caller omits
// stream_options the final chunk lacks usage. The recorder still finalizes,
// but with zero cost + unknown confidence.
func TestStream_OpenAI_NoUsageRecordsZeroCost(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := strings.Join([]string{
		`data: {"id":"a","model":"gpt-4o","choices":[{"delta":{"content":"hi"}}]}`,
		``,
		`data: [DONE]`,
		``,
		``,
	}, "\n")

	rec := clients.NewOpenAIStreamRecorder(newFakeBody(sse), buf, engine, taskID, "gpt-4o-fallback")
	drain(t, rec)

	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if !ev.CostUSD.Equal(decimal.Zero) {
		t.Errorf("expected zero cost, got %s", ev.CostUSD)
	}
	if ev.CostConfidence != core.CostConfidenceUnknown {
		t.Errorf("expected unknown confidence, got %s", ev.CostConfidence)
	}
	// Model from chunk wins over the fallback.
	if ev.Model != "gpt-4o" {
		t.Errorf("model: got %s want gpt-4o (chunk value should win)", ev.Model)
	}
}

// TestStream_Anthropic_RecordsTypedEvents — message_start carries model +
// initial input_tokens, message_delta carries cumulative output_tokens.
func TestStream_Anthropic_RecordsTypedEvents(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := strings.Join([]string{
		`event: message_start`,
		`data: {"type":"message_start","message":{"id":"m","model":"claude-3-5-sonnet-20240620","usage":{"input_tokens":12,"cache_read_input_tokens":4}}}`,
		``,
		`event: content_block_delta`,
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}`,
		``,
		`event: message_delta`,
		`data: {"type":"message_delta","usage":{"output_tokens":9}}`,
		``,
		`event: message_stop`,
		`data: {"type":"message_stop"}`,
		``,
		``,
	}, "\n")

	rec := clients.NewAnthropicStreamRecorder(newFakeBody(sse), buf, engine, taskID, "claude-fallback")
	drain(t, rec)

	in, out, cached := rec.Usage()
	if in != 12 || out != 9 || cached != 4 {
		t.Fatalf("usage: got in=%d out=%d cached=%d want 12/9/4", in, out, cached)
	}

	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	ev := events[0]
	if ev.Provider != "anthropic" {
		t.Errorf("provider: got %s want anthropic", ev.Provider)
	}
	if ev.Model != "claude-3-5-sonnet-20240620" {
		t.Errorf("model: got %s want claude-3-5-sonnet-20240620", ev.Model)
	}
	if ev.CachedTokens == nil || *ev.CachedTokens != 4 {
		t.Errorf("cached_tokens: got %v want 4", ev.CachedTokens)
	}
}

// TestStream_Groq_StampsProvider — Groq is wire-compatible with OpenAI; the
// recorder must still stamp provider="groq".
func TestStream_Groq_StampsProvider(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := strings.Join([]string{
		`data: {"model":"llama-3.1-70b-versatile","usage":{"prompt_tokens":3,"completion_tokens":2}}`,
		``,
		``,
	}, "\n")

	rec := clients.NewGroqStreamRecorder(newFakeBody(sse), buf, engine, taskID, "llama-3.1-70b-versatile")
	drain(t, rec)

	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Provider != "groq" {
		t.Errorf("provider: got %s want groq", events[0].Provider)
	}
}

// TestStream_FallbackModel — when no chunk carries a model field, the
// recorder uses the requestModel passed at construction time.
func TestStream_FallbackModel(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n"
	rec := clients.NewOpenAIStreamRecorder(newFakeBody(sse), buf, engine, taskID, "gpt-3.5-fallback")
	drain(t, rec)

	if rec.Model() != "gpt-3.5-fallback" {
		t.Errorf("Model(): got %s want gpt-3.5-fallback", rec.Model())
	}
	events, _ := buf.QueryEvents(taskID.String())
	if events[0].Model != "gpt-3.5-fallback" {
		t.Errorf("event model: got %s want gpt-3.5-fallback", events[0].Model)
	}
}

// TestStream_CloseFinalizesEarly — closing before EOF still produces an event
// and forwards Close to the underlying body.
func TestStream_CloseFinalizesEarly(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := "data: {\"model\":\"gpt-4o\",\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1}}\n\n"
	body := newFakeBody(sse)
	rec := clients.NewOpenAIStreamRecorder(body, buf, engine, taskID, "gpt-4o")

	// Read the first chunk only; do not consume EOF.
	tmp := make([]byte, len(sse))
	if _, err := rec.Read(tmp); err != nil && !errors.Is(err, io.EOF) {
		t.Fatalf("Read: %v", err)
	}
	if err := rec.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if !body.closed {
		t.Error("underlying body was not closed")
	}

	// Second close is a no-op.
	if err := rec.Close(); err != nil {
		t.Errorf("second Close should be no-op, got %v", err)
	}

	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected exactly 1 event, got %d", len(events))
	}
}

// TestStream_TransparentPassThrough_PartialReads — bytes returned by Read
// should match the underlying body byte-for-byte across multiple small reads.
func TestStream_TransparentPassThrough_PartialReads(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := "data: {\"model\":\"gpt-4o\",\"choices\":[{\"delta\":{\"content\":\"abc\"}}]}\n\n" +
		"data: {\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":1}}\n\n"
	rec := clients.NewOpenAIStreamRecorder(newFakeBody(sse), buf, engine, taskID, "gpt-4o")

	var out []byte
	chunk := make([]byte, 8)
	for {
		n, err := rec.Read(chunk)
		if n > 0 {
			out = append(out, chunk[:n]...)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("Read: %v", err)
		}
	}
	if string(out) != sse {
		t.Errorf("byte stream mismatch — got %q want %q", out, sse)
	}
	in, outTok, _ := rec.Usage()
	if in != 2 || outTok != 1 {
		t.Fatalf("usage across partial reads broke parsing: got %d/%d", in, outTok)
	}
}

// TestStream_AnthropicMessageDeltaInputFallback — when message_start is
// missing but message_delta carries `input_tokens`, the recorder still
// captures the input count (defensive parsing for partial streams).
func TestStream_AnthropicMessageDeltaInputFallback(t *testing.T) {
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	sse := "data: {\"type\":\"message_delta\",\"usage\":{\"input_tokens\":4,\"output_tokens\":3}}\n\n"
	rec := clients.NewAnthropicStreamRecorder(newFakeBody(sse), buf, engine, taskID, "claude-fallback")
	drain(t, rec)

	in, out, _ := rec.Usage()
	if in != 4 || out != 3 {
		t.Fatalf("usage: got %d/%d want 4/3", in, out)
	}
}

// Ensure StreamRecorder implements io.ReadCloser at the type level.
var _ io.ReadCloser = (*clients.StreamRecorder)(nil)

// TestStream_ContextlessUsage_PreserveDecimalZero — finalize path with no
// tokens must use decimal.Zero (regression guard for the early build break).
func TestStream_ContextlessUsage_PreserveDecimalZero(t *testing.T) {
	_ = context.Background() // keep import grounded for test variants below
	buf := newTestBuffer(t)
	engine := newTestPricingEngine(t)
	taskID := uuid.New()
	seedTask(t, buf, taskID)

	rec := clients.NewOpenAIStreamRecorder(newFakeBody(""), buf, engine, taskID, "")
	drain(t, rec)

	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if !events[0].CostUSD.Equal(decimal.Zero) {
		t.Errorf("expected decimal.Zero, got %s", events[0].CostUSD)
	}
}
