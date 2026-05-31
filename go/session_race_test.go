// Sprint 2 Theme D / §3.2.2 (B13) regression — SessionManager race.
//
// Pre-fix `GetOrCreateSessionForIdentity` does a lookup-then-create
// with the mutex RELEASED between the two phases:
//
//   lock → for-loop scan (no match) → unlock
//   (CreateAutoTask + Buffer.InsertTask run unlocked)
//   lock → insert under fresh nextID → unlock
//
// Two concurrent callers with the same identity both miss the lookup,
// both create tasks, both insert under different IDs → duplicate
// sessions, duplicate buffer InsertTask upserts. Customer dashboards
// show two sessions when there should be one.

package dexcost

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// TestGetOrCreateSessionForIdentity_Race must be run under `go test -race`
// AND functionally (assert identical TaskID across all goroutines).
func TestGetOrCreateSessionForIdentity_Race(t *testing.T) {
	sm := NewSessionManager(5 * time.Minute)

	// Same identity for every caller — only one session task should exist.
	ctx := core.SetContext(context.Background(), &core.ContextData{
		CustomerID: "cust-1",
		ProjectID:  "proj-1",
		Agent:      "ag-1",
	})

	const N = 100
	results := make([]*core.Task, N)
	start := make(chan struct{}) // barrier
	var wg sync.WaitGroup
	wg.Add(N)
	for i := 0; i < N; i++ {
		go func(i int) {
			defer wg.Done()
			<-start // block until released — all goroutines race past at once
			results[i] = sm.GetOrCreateSessionForIdentity(ctx, "http", nil)
		}(i)
	}
	close(start) // unleash
	wg.Wait()

	first := results[0]
	if first == nil {
		t.Fatal("first goroutine got nil task")
	}
	for i := 1; i < N; i++ {
		if results[i] == nil {
			t.Errorf("goroutine %d got nil task", i)
			continue
		}
		if results[i].TaskID != first.TaskID {
			t.Errorf(
				"race detected: goroutine %d got TaskID=%s, "+
					"expected %s (single session per identity)",
				i, results[i].TaskID, first.TaskID,
			)
		}
	}
}
