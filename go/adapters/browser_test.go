package adapters_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/adapters"
	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/transport"
	"github.com/shopspring/decimal"
)

// browserEvents returns the compute_cost events recorded by the browser adapter.
func browserEvents() []core.Event {
	var out []core.Event
	for _, e := range adapters.GetRecordedEvents() {
		if e.EventType == core.EventTypeComputeCost {
			out = append(out, e)
		}
	}
	return out
}

// TestBrowserSession_RecordsComputeCost verifies a timed browser session
// records a compute_cost event against the active task.
func TestBrowserSession_RecordsComputeCost(t *testing.T) {
	adapters.ClearRecordedEvents()
	adapters.SetEventBuffer(nil)

	task := core.NewTask("scrape")
	ctx := core.WithTask(context.Background(), &task)

	bs := adapters.StartBrowserSession(ctx, "https://example.com", decimal.NewFromFloat(0.60))
	time.Sleep(20 * time.Millisecond)
	bs.End()

	events := browserEvents()
	if len(events) != 1 {
		t.Fatalf("expected 1 compute_cost event, got %d", len(events))
	}
	ev := events[0]
	if ev.ServiceName != "playwright_browser" {
		t.Errorf("expected service_name playwright_browser, got %s", ev.ServiceName)
	}
	if ev.CostUSD.IsZero() || ev.CostUSD.IsNegative() {
		t.Errorf("expected positive cost, got %s", ev.CostUSD)
	}
	if ev.Details["page_url"] != "https://example.com" {
		t.Errorf("expected page_url in details, got %v", ev.Details["page_url"])
	}
}

// TestBrowserSession_NoTaskNoOp verifies the adapter is a silent no-op when
// there is no active task (Python parity).
func TestBrowserSession_NoTaskNoOp(t *testing.T) {
	adapters.ClearRecordedEvents()
	adapters.SetEventBuffer(nil)

	bs := adapters.StartBrowserSession(context.Background(), "https://example.com", decimal.Zero)
	bs.End()

	if got := len(browserEvents()); got != 0 {
		t.Errorf("expected no events without an active task, got %d", got)
	}
}

// TestBrowserSession_EndIdempotent verifies a second End is a no-op.
func TestBrowserSession_EndIdempotent(t *testing.T) {
	adapters.ClearRecordedEvents()
	adapters.SetEventBuffer(nil)

	task := core.NewTask("scrape")
	ctx := core.WithTask(context.Background(), &task)

	bs := adapters.StartBrowserSession(ctx, "https://example.com", decimal.NewFromFloat(0.60))
	bs.End()
	bs.End()

	if got := len(browserEvents()); got != 1 {
		t.Errorf("expected exactly 1 event after double End, got %d", got)
	}
}

// TestTrackBrowser_RecordsOnError verifies the callback wrapper records the
// cost even when the wrapped work returns an error.
func TestTrackBrowser_RecordsOnError(t *testing.T) {
	adapters.ClearRecordedEvents()
	adapters.SetEventBuffer(nil)

	task := core.NewTask("scrape")
	ctx := core.WithTask(context.Background(), &task)

	wantErr := errors.New("navigation failed")
	err := adapters.TrackBrowser(ctx, "https://example.com", decimal.NewFromFloat(0.60), func() error {
		time.Sleep(10 * time.Millisecond)
		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Errorf("expected the wrapped error to propagate, got %v", err)
	}
	if got := len(browserEvents()); got != 1 {
		t.Errorf("expected the cost to be recorded despite the error, got %d events", got)
	}
}

// TestBrowserSession_PersistsToBuffer verifies the compute_cost event reaches
// durable storage so the sync pusher can ship it.
func TestBrowserSession_PersistsToBuffer(t *testing.T) {
	adapters.ClearRecordedEvents()

	dbPath := filepath.Join(t.TempDir(), "browser_persist.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	defer buf.Close()
	adapters.SetEventBuffer(buf)
	defer adapters.SetEventBuffer(nil)

	task := core.NewTask("scrape")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("InsertTask: %v", err)
	}
	ctx := core.WithTask(context.Background(), &task)

	bs := adapters.StartBrowserSession(ctx, "https://example.com", decimal.NewFromFloat(0.60))
	time.Sleep(20 * time.Millisecond)
	bs.End()

	stored, err := buf.QueryEvents(task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(stored) != 1 {
		t.Fatalf("expected 1 event persisted to storage, got %d", len(stored))
	}
	if stored[0].EventType != core.EventTypeComputeCost {
		t.Errorf("expected compute_cost, got %s", stored[0].EventType)
	}
}
