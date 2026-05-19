package transport

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// makeTestEventWithPadding creates an event whose Details map contains a
// "padding" key of the given size. Note: the pusher's EnforceMetadataLimit
// caps per-event details at 10KB, so detailsSize should stay <=8000 to
// survive the security pass intact.
func makeTestEventWithPadding(t *testing.T, taskID core.Task, detailsSize int) core.Event {
	t.Helper()
	e := core.NewEvent(taskID.TaskID, core.EventTypeLLMCall)
	e.CostUSD = decimal.NewFromFloat(0.05)
	e.Provider = "openai"
	e.Model = "gpt-4"
	if detailsSize > 0 {
		padding := strings.Repeat("x", detailsSize)
		e.Details["padding"] = padding
	}
	return e
}

// setupSplitPusher creates a pusher backed by a SQLite buffer that points at
// the supplied httptest.Server. The flush interval is set very high so tests
// control flushes explicitly.
func setupSplitPusher(t *testing.T, handler http.HandlerFunc) (*SQLiteBuffer, *EventPusher, *httptest.Server) {
	t.Helper()
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("create buffer: %v", err)
	}

	srv := httptest.NewServer(handler)

	p := NewEventPusher(PusherOptions{
		Buffer:    buf,
		Endpoint:  srv.URL,
		APIKey:    "dx_test_split_123",
		BatchSize: 200, // high so all events come out in one pushBatch call
		Interval:  1 * time.Hour,
	})

	t.Cleanup(func() {
		p.Stop()
		srv.Close()
		buf.Close()
	})

	return buf, p, srv
}

// ---------------------------------------------------------------------------
// Tests: Constants
// ---------------------------------------------------------------------------

func TestMaxPayloadBytesConstant(t *testing.T) {
	if maxPayloadBytes != 200_000 {
		t.Errorf("expected maxPayloadBytes=200000, got %d", maxPayloadBytes)
	}
	if maxPayloadBytes >= 256_000 {
		t.Error("maxPayloadBytes must be under SQS 256KB limit")
	}
}

func TestMaxSplitDepthConstant(t *testing.T) {
	if maxSplitDepth != 5 {
		t.Errorf("expected maxSplitDepth=5, got %d", maxSplitDepth)
	}
}

// ---------------------------------------------------------------------------
// Tests: End-to-end via Flush (exercises pushBatch -> pushWithSplit)
// ---------------------------------------------------------------------------

// A small batch that fits easily under 200KB should result in exactly one HTTP
// request.
func TestPushWithSplit_SmallBatch_SingleRequest(t *testing.T) {
	var mu sync.Mutex
	requestCount := 0
	var totalEvents int

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)

		mu.Lock()
		requestCount++
		if evts, ok := payload["events"].([]interface{}); ok {
			totalEvents += len(evts)
		}
		mu.Unlock()

		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]bool{"accepted": true})
	}

	buf, p, _ := setupSplitPusher(t, handler)

	task := core.NewTask("split_test")
	buf.InsertTask(task)

	// 5 small events — well under 200KB.
	for i := 0; i < 5; i++ {
		e := makeTestEventWithPadding(t, task, 0) // no padding
		buf.InsertEvent(e)
	}

	if err := p.Flush(); err != nil {
		t.Fatalf("flush failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if requestCount != 1 {
		t.Errorf("expected 1 HTTP request, got %d", requestCount)
	}
	if totalEvents != 5 {
		t.Errorf("expected 5 events total, got %d", totalEvents)
	}
}

// A batch whose serialized size exceeds maxPayloadBytes should be split into
// multiple HTTP requests.
//
// EnforceMetadataLimit caps details at 10KB per event, so we use 8KB padding
// (under the limit) and 25 events to reach ~207KB total > 200KB threshold.
func TestPushWithSplit_LargeBatch_MultipleRequests(t *testing.T) {
	var mu sync.Mutex
	requestCount := 0
	var totalEvents int

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)

		mu.Lock()
		requestCount++
		if evts, ok := payload["events"].([]interface{}); ok {
			totalEvents += len(evts)
		}
		mu.Unlock()

		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupSplitPusher(t, handler)

	task := core.NewTask("split_test")
	buf.InsertTask(task)

	// 25 events with 8KB details each. After metadata enforcement (10KB cap)
	// the padding survives intact. 25 * ~8.5KB ≈ 207KB > 200KB → must split.
	for i := 0; i < 25; i++ {
		e := makeTestEventWithPadding(t, task, 8_000)
		buf.InsertEvent(e)
	}

	if err := p.Flush(); err != nil {
		t.Fatalf("flush failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if requestCount < 2 {
		t.Errorf("expected >=2 HTTP requests (split), got %d", requestCount)
	}
	if totalEvents != 25 {
		t.Errorf("expected 25 events total across splits, got %d", totalEvents)
	}
}

// When the server returns 413, the pusher should NOT retry the same batch
// (the error is permanent, not transient).
func TestPushWithSplit_413_NotRetried(t *testing.T) {
	var mu sync.Mutex
	requestCount := 0

	handler := func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		requestCount++
		mu.Unlock()

		w.WriteHeader(http.StatusRequestEntityTooLarge)
		json.NewEncoder(w).Encode(map[string]string{"error": "Payload too large"})
	}

	buf, p, _ := setupSplitPusher(t, handler)

	task := core.NewTask("split_test")
	buf.InsertTask(task)

	e := makeTestEventWithPadding(t, task, 100)
	buf.InsertEvent(e)

	// Flush should return an error (413 is permanent).
	err := p.Flush()
	if err == nil {
		t.Fatal("expected error from 413 response")
	}
	if !strings.Contains(err.Error(), "413") {
		t.Errorf("expected error to mention 413, got: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	// Only 1 request — the pusher must not retry a 413.
	if requestCount != 1 {
		t.Errorf("expected exactly 1 request (no retry on 413), got %d", requestCount)
	}
}

// ---------------------------------------------------------------------------
// Tests: pushWithSplit unit tests (bypass pushBatch to avoid metadata limits)
// ---------------------------------------------------------------------------

// A single event whose serialized form exceeds maxPayloadBytes cannot be split
// further and should be skipped (logged, not sent). We call pushWithSplit
// directly to bypass the metadata enforcement in pushBatch.
func TestPushWithSplit_SingleOversizedEvent_Skipped(t *testing.T) {
	var mu sync.Mutex
	requestCount := 0

	handler := func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		requestCount++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}

	_, p, _ := setupSplitPusher(t, handler)

	// Build a single event dict with 250KB padding (exceeds 200KB limit).
	eventDict := map[string]interface{}{
		"event_id":   "e1111111-1111-1111-1111-111111111111",
		"task_id":    "t1111111-1111-1111-1111-111111111111",
		"event_type": "llm_call",
		"cost_usd":   "0.05",
		"details":    map[string]interface{}{"padding": strings.Repeat("x", 250_000)},
	}

	// Call pushWithSplit directly — single event should be skipped.
	err := p.pushWithSplit([]map[string]interface{}{eventDict}, nil, 0)
	if err != nil {
		t.Fatalf("expected no error for skipped oversized event, got: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	// The single oversized event is skipped, so no HTTP request is made.
	if requestCount != 0 {
		t.Errorf("expected 0 requests (oversized event skipped), got %d", requestCount)
	}
}

// Verify that pushWithSplit splits a batch of directly-constructed large
// event dicts into multiple requests.
func TestPushWithSplit_DirectCall_Splits(t *testing.T) {
	var mu sync.Mutex
	requestCount := 0
	var totalEvents int

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)

		mu.Lock()
		requestCount++
		if evts, ok := payload["events"].([]interface{}); ok {
			totalEvents += len(evts)
		}
		mu.Unlock()

		w.WriteHeader(http.StatusOK)
	}

	_, p, _ := setupSplitPusher(t, handler)

	// Build 10 event dicts with 30KB padding each ≈ 300KB total.
	events := make([]map[string]interface{}, 10)
	for i := 0; i < 10; i++ {
		events[i] = map[string]interface{}{
			"event_id":   strings.Replace("e1111111-1111-1111-1111-111111111111", "e1", "e"+string(rune('a'+i)), 1),
			"task_id":    "t1111111-1111-1111-1111-111111111111",
			"event_type": "llm_call",
			"cost_usd":   "0.05",
			"details":    map[string]interface{}{"padding": strings.Repeat("x", 30_000)},
		}
	}

	err := p.pushWithSplit(events, nil, 0)
	if err != nil {
		t.Fatalf("pushWithSplit failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if requestCount < 2 {
		t.Errorf("expected >=2 HTTP requests from direct split, got %d", requestCount)
	}
	if totalEvents != 10 {
		t.Errorf("expected 10 events total, got %d", totalEvents)
	}
}

// Verify that each split sub-request carries the correct Authorization header.
func TestPushWithSplit_AuthHeaderOnAllRequests(t *testing.T) {
	var mu sync.Mutex
	var authHeaders []string

	handler := func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		authHeaders = append(authHeaders, r.Header.Get("Authorization"))
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}

	_, p, _ := setupSplitPusher(t, handler)

	// Build event dicts that force a split.
	events := make([]map[string]interface{}, 10)
	for i := 0; i < 10; i++ {
		events[i] = map[string]interface{}{
			"event_id":   "e" + strings.Repeat(string(rune('0'+i)), 7) + "-1111-1111-1111-111111111111",
			"task_id":    "t1111111-1111-1111-1111-111111111111",
			"event_type": "llm_call",
			"cost_usd":   "0.05",
			"details":    map[string]interface{}{"padding": strings.Repeat("x", 30_000)},
		}
	}

	if err := p.pushWithSplit(events, nil, 0); err != nil {
		t.Fatalf("pushWithSplit failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(authHeaders) < 2 {
		t.Fatalf("expected >=2 requests, got %d", len(authHeaders))
	}
	for i, hdr := range authHeaders {
		expected := "Bearer dx_test_split_123"
		if hdr != expected {
			t.Errorf("request %d: expected auth=%q, got %q", i, expected, hdr)
		}
	}
}

// Verify that the total number of events received across split requests equals
// the number of events we submitted, even with odd counts.
func TestPushWithSplit_OddEventCount_AllDelivered(t *testing.T) {
	var mu sync.Mutex
	var totalEvents int

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)

		mu.Lock()
		if evts, ok := payload["events"].([]interface{}); ok {
			totalEvents += len(evts)
		}
		mu.Unlock()

		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupSplitPusher(t, handler)

	task := core.NewTask("split_test")
	buf.InsertTask(task)

	// 27 events with 8KB details each ≈ 229KB → split needed, odd count.
	for i := 0; i < 27; i++ {
		e := makeTestEventWithPadding(t, task, 8_000)
		buf.InsertEvent(e)
	}

	if err := p.Flush(); err != nil {
		t.Fatalf("flush failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if totalEvents != 27 {
		t.Errorf("expected 27 events total, got %d", totalEvents)
	}
}

// Tasks should only accompany the first sub-batch in a split to avoid
// duplicating task upserts.
func TestPushWithSplit_TasksOnlyInFirstHalf(t *testing.T) {
	var mu sync.Mutex
	type reqData struct {
		events int
		tasks  int
	}
	var requests []reqData

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)

		mu.Lock()
		rd := reqData{}
		if evts, ok := payload["events"].([]interface{}); ok {
			rd.events = len(evts)
		}
		if tks, ok := payload["tasks"].([]interface{}); ok {
			rd.tasks = len(tks)
		}
		requests = append(requests, rd)
		mu.Unlock()

		w.WriteHeader(http.StatusOK)
	}

	_, p, _ := setupSplitPusher(t, handler)

	// Events that force a split.
	events := make([]map[string]interface{}, 10)
	for i := 0; i < 10; i++ {
		events[i] = map[string]interface{}{
			"event_id":   "e" + strings.Repeat(string(rune('0'+i)), 7) + "-1111-1111-1111-111111111111",
			"task_id":    "t1111111-1111-1111-1111-111111111111",
			"event_type": "llm_call",
			"cost_usd":   "0.05",
			"details":    map[string]interface{}{"padding": strings.Repeat("x", 30_000)},
		}
	}
	tasks := []map[string]interface{}{
		{"task_id": "t1111111-1111-1111-1111-111111111111", "task_type": "test"},
	}

	if err := p.pushWithSplit(events, tasks, 0); err != nil {
		t.Fatalf("pushWithSplit failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(requests) < 2 {
		t.Fatalf("expected >=2 requests, got %d", len(requests))
	}
	// First request should carry the task(s).
	if requests[0].tasks != 1 {
		t.Errorf("first request should have 1 task, got %d", requests[0].tasks)
	}
	// Subsequent requests should have 0 tasks.
	for i := 1; i < len(requests); i++ {
		if requests[i].tasks != 0 {
			t.Errorf("request %d should have 0 tasks, got %d", i, requests[i].tasks)
		}
	}
}
