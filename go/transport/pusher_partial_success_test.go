// Sprint 2 Theme D / §3.2.1 (B12) regression — Go pusher partial
// success accounting.
//
// Pre-fix the outer pushBatch called MarkSynced AFTER pushWithSplit
// returned success. If the first half POST succeeded but the second
// half returned 5xx, pushWithSplit bubbled the error up and the
// outer MarkSynced never ran → events that DID reach the control
// plane were re-pushed next tick → duplicates server-side.

package transport

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func TestPushWithSplit_PartialFailure_FirstHalfMarkedSynced(t *testing.T) {
	// Seed buffer with enough events to force a split. We use 4 events
	// with 8 KB padding each → ~32 KB raw; with the small payload limit
	// trick (set maxPayloadBytes via a tiny test endpoint? No, the
	// constant is package-level) we instead rely on the existing
	// pusher_split_test helpers which pad heavily.
	//
	// We use 200 events with detailsSize=4000 each (~800 KB) — well
	// over `maxPayloadBytes=512_000` (probe finding in pusher.go).
	// First split: 100 + 100. Each half is ~400 KB, fits. Two leaf
	// POSTs result.
	const eventCount = 200
	const detailsSize = 4000

	buf, err := NewSQLiteBuffer(t.TempDir() + "/buf.db")
	if err != nil {
		t.Fatalf("buf: %v", err)
	}
	defer buf.Close()

	taskID := uuid.New()
	task := core.NewTask("partial-fail-test")
	task.TaskID = taskID
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert task: %v", err)
	}
	for i := 0; i < eventCount; i++ {
		ev := core.NewEvent(taskID, core.EventTypeExternalCost)
		ev.CostUSD = decimal.NewFromFloat(0.001)
		ev.Details = map[string]interface{}{
			"padding": strings.Repeat("x", detailsSize),
		}
		if err := buf.InsertEvent(ev); err != nil {
			t.Fatalf("insert event %d: %v", i, err)
		}
	}

	// HTTP test server: first request 200, second request 500.
	var callCount int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&callCount, 1)
		if n == 1 {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"queued": 100}`))
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	p := NewEventPusher(PusherOptions{
		Buffer:    buf,
		Endpoint:  server.URL,
		APIKey:    "dx_test_x",
		BatchSize: eventCount,
		Interval:  time.Hour, // no auto-tick; we drive via Flush
	})
	p.Flush()

	if calls := atomic.LoadInt32(&callCount); calls < 2 {
		t.Fatalf("expected at least 2 POSTs (split into halves), got %d", calls)
	}

	// Verify: after partial failure, exactly the first-half count of
	// events is marked synced. Pre-fix: 0 (outer MarkSynced never ran).
	// Post-fix: 100 (first leaf marked them).
	pending, err := buf.QueryPendingEvents(eventCount)
	if err != nil {
		t.Fatalf("get pending: %v", err)
	}
	if len(pending) == eventCount {
		t.Fatalf("partial-failure regression: ALL %d events still pending "+
			"(first-half POST succeeded but was not marked synced — will "+
			"be duplicated on next tick)", eventCount)
	}
	if len(pending) == 0 {
		t.Fatalf("second-half failure was silently swallowed; expected "+
			"~%d events to remain pending", eventCount/2)
	}
	// Allow some slack (the split may not be exactly 50/50 if the
	// recursion descends further) — but it shouldn't be ALL or NONE.
	t.Logf("pending after partial failure: %d / %d (first half marked synced)",
		len(pending), eventCount)
}
