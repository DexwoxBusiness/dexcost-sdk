package transport

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func setupPusherTest(t *testing.T, handler http.HandlerFunc) (*SQLiteBuffer, *EventPusher, *httptest.Server) {
	t.Helper()
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("create buffer: %v", err)
	}

	srv := httptest.NewServer(handler)

	p := NewEventPusher(PusherOptions{
		Buffer:    buf,
		Endpoint:  srv.URL,
		APIKey:    "dx_test_abc123",
		BatchSize: 100,
		Interval:  1 * time.Hour, // long interval so we control flushes
	})

	t.Cleanup(func() {
		p.Stop()
		srv.Close()
		buf.Close()
	})

	return buf, p, srv
}

func TestPusher_SendsBatch(t *testing.T) {
	var mu sync.Mutex
	var received []map[string]interface{}

	handler := func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]interface{}
		json.Unmarshal(body, &payload)
		mu.Lock()
		events, ok := payload["events"].([]interface{})
		if ok {
			for _, e := range events {
				if em, ok := e.(map[string]interface{}); ok {
					received = append(received, em)
				}
			}
		}
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupPusherTest(t, handler)

	// Insert a task first to satisfy the FK constraint, then insert events.
	task := core.NewTask("test_task")
	buf.InsertTask(task)
	taskID := task.TaskID
	e1 := core.NewEvent(taskID, core.EventTypeLLMCall)
	e1.CostUSD = decimal.NewFromFloat(0.01)
	e2 := core.NewEvent(taskID, core.EventTypeExternalCost)
	e2.CostUSD = decimal.NewFromFloat(0.005)
	buf.InsertEvent(e1)
	buf.InsertEvent(e2)

	// Flush.
	if err := p.Flush(); err != nil {
		t.Fatalf("flush failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(received) != 2 {
		t.Errorf("expected 2 events, got %d", len(received))
	}
}

func TestPusher_MarksEventsSynced(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupPusherTest(t, handler)

	task := core.NewTask("test_task")
	buf.InsertTask(task)
	taskID := task.TaskID
	e1 := core.NewEvent(taskID, core.EventTypeLLMCall)
	buf.InsertEvent(e1)

	// Verify pending before flush.
	pending, _ := buf.QueryPendingEvents(100)
	if len(pending) != 1 {
		t.Fatalf("expected 1 pending, got %d", len(pending))
	}

	p.Flush()

	// Verify marked as synced.
	pending2, _ := buf.QueryPendingEvents(100)
	if len(pending2) != 0 {
		t.Errorf("expected 0 pending after flush, got %d", len(pending2))
	}
}

func TestPusher_ExponentialBackoff(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}

	buf, p, _ := setupPusherTest(t, handler)

	task := core.NewTask("test_task")
	buf.InsertTask(task)
	taskID := task.TaskID
	buf.InsertEvent(core.NewEvent(taskID, core.EventTypeLLMCall))

	// First failure.
	p.Flush()
	if p.Backoff() != 1*time.Second {
		t.Errorf("expected 1s backoff, got %v", p.Backoff())
	}

	// Second failure.
	p.Flush()
	if p.Backoff() != 2*time.Second {
		t.Errorf("expected 2s backoff, got %v", p.Backoff())
	}

	// Third failure.
	p.Flush()
	if p.Backoff() != 4*time.Second {
		t.Errorf("expected 4s backoff, got %v", p.Backoff())
	}
}

func TestPusher_ResetsBackoffOnSuccess(t *testing.T) {
	callCount := 0
	var mu sync.Mutex
	handler := func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		callCount++
		c := callCount
		mu.Unlock()
		if c <= 2 {
			w.WriteHeader(http.StatusInternalServerError)
		} else {
			w.WriteHeader(http.StatusOK)
		}
	}

	buf, p, _ := setupPusherTest(t, handler)

	task := core.NewTask("test_task")
	buf.InsertTask(task)
	taskID := task.TaskID
	buf.InsertEvent(core.NewEvent(taskID, core.EventTypeLLMCall))

	// Two failures.
	p.Flush()
	p.Flush()
	if p.Backoff() == 0 {
		t.Error("expected non-zero backoff after failures")
	}

	// Success resets.
	p.Flush()
	if p.Backoff() != 0 {
		t.Errorf("expected 0 backoff after success, got %v", p.Backoff())
	}
}

func TestPusher_AuthHeader(t *testing.T) {
	var authHeader string
	var mu sync.Mutex
	handler := func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		authHeader = r.Header.Get("Authorization")
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupPusherTest(t, handler)

	task := core.NewTask("test_task")
	buf.InsertTask(task)
	taskID := task.TaskID
	buf.InsertEvent(core.NewEvent(taskID, core.EventTypeLLMCall))
	p.Flush()

	mu.Lock()
	defer mu.Unlock()
	expected := "Bearer dx_test_abc123"
	if authHeader != expected {
		t.Errorf("expected auth=%q, got %q", expected, authHeader)
	}
}

func TestPusher_Flush_NoEvents(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}

	_, p, _ := setupPusherTest(t, handler)

	// Flush with no events should not error.
	if err := p.Flush(); err != nil {
		t.Errorf("unexpected error on empty flush: %v", err)
	}
}

// TestPusher_StartStop_Idempotent verifies that the EventPusher's lifecycle
// methods (added in DEX-266) are idempotent and goroutine-safe:
//
//  1. Calling Stop() multiple times does not panic and does not deadlock.
//  2. After Stop() returns, calling Start() resumes the background pusher
//     such that Flush() can drive a successful round-trip again.
//  3. Calling Start() while already running is a no-op (does not spawn a
//     duplicate goroutine).
//  4. Stop() called immediately after Start() does not leak the run() goroutine
//     (regression test for the pre-DEX-266 race where running.Store(true) was
//     set inside run() after `go p.run()`).
func TestPusher_StartStop_Idempotent(t *testing.T) {
	var hits int
	var hitsMu sync.Mutex
	handler := func(w http.ResponseWriter, r *http.Request) {
		hitsMu.Lock()
		hits++
		hitsMu.Unlock()
		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupPusherTest(t, handler)

	// 1. Stop is idempotent: two Stop() calls in a row must not panic.
	p.Stop()
	p.Stop()

	// 2. Start re-spawns the goroutine; Flush must succeed end-to-end.
	p.Start()
	task := core.NewTask("test_restart")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("InsertTask: %v", err)
	}
	ev := core.NewEvent(task.TaskID, core.EventTypeExternalCost)
	ev.CostUSD = decimal.NewFromFloat(0.01)
	if err := buf.InsertEvent(ev); err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}
	if err := p.Flush(); err != nil {
		t.Fatalf("Flush after restart: %v", err)
	}

	hitsMu.Lock()
	first := hits
	hitsMu.Unlock()
	if first == 0 {
		t.Fatal("expected at least one ingest hit after Start()+Flush()")
	}

	// 3. Start while running is a no-op. Calling it again must not spawn a
	// second goroutine; we verify by calling Stop() once and checking that
	// the goroutine count returns to the steady state (i.e. wg.Wait() in
	// Stop() returns instead of hanging).
	p.Start()
	p.Start()

	// 4. Stop must clean up the single run() goroutine. If a duplicate had
	// been spawned, p.wg.Wait() inside Stop() would deadlock past this point;
	// the test framework will fire the timeout and fail.
	done := make(chan struct{})
	go func() {
		p.Stop()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop() deadlocked — likely a leaked run() goroutine from duplicate Start()")
	}

	// 5. Stop() after final Stop() is still safe.
	p.Stop()
}

// TestPusher_StartImmediatelyAfterStop guards the race fixed in DEX-266 where
// running.Store(true) was previously set inside run(); a Stop() racing against
// the goroutine startup could observe running==false and short-circuit,
// leaking the goroutine. After the fix Start() sets running before spawning,
// so a back-to-back Stop()+Start()+Stop() must never deadlock.
func TestPusher_StartImmediatelyAfterStop(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}
	_, p, _ := setupPusherTest(t, handler)

	for i := 0; i < 10; i++ {
		p.Stop()
		p.Start()
	}

	done := make(chan struct{})
	go func() {
		p.Stop()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop() deadlocked after 10 Stop+Start cycles")
	}
}
