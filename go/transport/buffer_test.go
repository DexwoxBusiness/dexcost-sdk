package transport

import (
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func tempDB(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	return filepath.Join(dir, "test.db")
}

func TestNewSQLiteBuffer_CreatesDB(t *testing.T) {
	path := tempDB(t)
	buf, err := NewSQLiteBuffer(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()
	if _, err := os.Stat(path); os.IsNotExist(err) {
		t.Error("expected database file to be created")
	}
}

func TestSQLiteBuffer_InsertAndQueryTask(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("resolve_ticket")
	task.CustomerID = "acme"
	task.TotalCostUSD = decimal.NewFromFloat(0.05)

	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	got, err := buf.GetTask(task.TaskID.String())
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if got == nil {
		t.Fatal("expected task, got nil")
	}
	if got.CustomerID != "acme" {
		t.Errorf("expected customer_id=acme, got %s", got.CustomerID)
	}
	if !got.TotalCostUSD.Equal(decimal.NewFromFloat(0.05)) {
		t.Errorf("expected total_cost=0.05, got %s", got.TotalCostUSD)
	}
}

func TestSQLiteBuffer_UpdateTask(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("test_update")
	buf.InsertTask(task)

	task.Status = core.TaskStatusSuccess
	task.TotalCostUSD = decimal.NewFromFloat(1.23)
	if err := buf.UpdateTask(task); err != nil {
		t.Fatalf("update failed: %v", err)
	}

	got, _ := buf.GetTask(task.TaskID.String())
	if got.Status != core.TaskStatusSuccess {
		t.Errorf("expected success, got %s", got.Status)
	}
	if !got.TotalCostUSD.Equal(decimal.NewFromFloat(1.23)) {
		t.Errorf("expected 1.23, got %s", got.TotalCostUSD)
	}
}

func TestSQLiteBuffer_InsertAndQueryEvent(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	// Insert parent task first to satisfy the FK constraint.
	parentTask := core.NewTask("test_task")
	if err := buf.InsertTask(parentTask); err != nil {
		t.Fatalf("insert task failed: %v", err)
	}
	taskID := parentTask.TaskID
	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.Provider = "openai"
	event.Model = "gpt-4"
	tokens := 100
	event.InputTokens = &tokens
	outTokens := 50
	event.OutputTokens = &outTokens
	event.CostUSD = decimal.NewFromFloat(0.003)

	if err := buf.InsertEvent(event); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("query failed: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Provider != "openai" {
		t.Errorf("expected provider=openai, got %s", events[0].Provider)
	}
	if !events[0].CostUSD.Equal(decimal.NewFromFloat(0.003)) {
		t.Errorf("expected cost=0.003, got %s", events[0].CostUSD)
	}
}

func TestSQLiteBuffer_PendingEventsAndMarkSynced(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	parentTask := core.NewTask("test_task")
	buf.InsertTask(parentTask)
	taskID := parentTask.TaskID
	e1 := core.NewEvent(taskID, core.EventTypeLLMCall)
	e2 := core.NewEvent(taskID, core.EventTypeExternalCost)
	buf.InsertEvent(e1)
	buf.InsertEvent(e2)

	pending, err := buf.QueryPendingEvents(100)
	if err != nil {
		t.Fatalf("query pending failed: %v", err)
	}
	if len(pending) != 2 {
		t.Fatalf("expected 2 pending, got %d", len(pending))
	}

	// Mark first as synced
	if err := buf.MarkSynced([]string{e1.EventID.String()}); err != nil {
		t.Fatalf("mark synced failed: %v", err)
	}

	pending2, _ := buf.QueryPendingEvents(100)
	if len(pending2) != 1 {
		t.Fatalf("expected 1 pending after sync, got %d", len(pending2))
	}
	if pending2[0].EventID != e2.EventID {
		t.Errorf("expected remaining event=%s, got %s", e2.EventID, pending2[0].EventID)
	}
}

func TestSQLiteBuffer_RetryFields(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	parentTask := core.NewTask("test_task")
	buf.InsertTask(parentTask)
	taskID := parentTask.TaskID
	origID := uuid.New()
	event := core.NewEvent(taskID, core.EventTypeRetryMarker)
	event.IsRetry = true
	event.RetryReason = "rate_limit"
	event.RetryOf = &origID
	event.CostUSD = decimal.NewFromFloat(0.01)

	buf.InsertEvent(event)
	events, _ := buf.QueryEvents(taskID.String())
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	e := events[0]
	if !e.IsRetry {
		t.Error("expected is_retry=true")
	}
	if e.RetryReason != "rate_limit" {
		t.Errorf("expected retry_reason=rate_limit, got %s", e.RetryReason)
	}
	if e.RetryOf == nil || *e.RetryOf != origID {
		t.Errorf("expected retry_of=%s", origID)
	}
}

func TestSQLiteBuffer_ExperimentFields(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("ab_test")
	task.ExperimentID = "exp-pricing-v2"
	task.Variant = "control"
	task.CustomerID = "cust-1"

	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	got, err := buf.GetTask(task.TaskID.String())
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if got == nil {
		t.Fatal("expected task, got nil")
	}
	if got.ExperimentID != "exp-pricing-v2" {
		t.Errorf("expected experiment_id=exp-pricing-v2, got %s", got.ExperimentID)
	}
	if got.Variant != "control" {
		t.Errorf("expected variant=control, got %s", got.Variant)
	}
}

func TestSQLiteBuffer_ExperimentFieldsEmpty(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("no_experiment")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	got, err := buf.GetTask(task.TaskID.String())
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if got.ExperimentID != "" {
		t.Errorf("expected empty experiment_id, got %s", got.ExperimentID)
	}
	if got.Variant != "" {
		t.Errorf("expected empty variant, got %s", got.Variant)
	}
}

func TestSQLiteBuffer_UpdateEvent(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	parentTask := core.NewTask("test_task")
	buf.InsertTask(parentTask)
	taskID := parentTask.TaskID
	origID := uuid.New()

	// 1. Insert an event with IsRetry=true, RetryReason="rate_limit", RetryOf=&origID
	event := core.NewEvent(taskID, core.EventTypeLLMCall)
	event.IsRetry = true
	event.RetryReason = "rate_limit"
	event.RetryOf = &origID
	event.CostUSD = decimal.NewFromFloat(0.05)

	if err := buf.InsertEvent(event); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	// 2. Modify: set IsRetry=false, RetryReason="", RetryOf=nil
	event.IsRetry = false
	event.RetryReason = ""
	event.RetryOf = nil

	// 3. Call UpdateEvent
	if err := buf.UpdateEvent(event); err != nil {
		t.Fatalf("update failed: %v", err)
	}

	// 4. QueryEvents and verify the changes persisted
	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("query failed: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	got := events[0]
	if got.IsRetry {
		t.Error("expected is_retry=false after update")
	}
	if got.RetryReason != "" {
		t.Errorf("expected retry_reason empty after update, got %q", got.RetryReason)
	}
	if got.RetryOf != nil {
		t.Errorf("expected retry_of=nil after update, got %s", got.RetryOf)
	}
	if !got.CostUSD.Equal(decimal.NewFromFloat(0.05)) {
		t.Errorf("expected cost=0.05 preserved, got %s", got.CostUSD)
	}
}

func TestSQLiteBuffer_DecimalPrecision(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("precision_test")
	task.TotalCostUSD = decimal.RequireFromString("0.123456789012345678")
	buf.InsertTask(task)

	got, _ := buf.GetTask(task.TaskID.String())
	if !got.TotalCostUSD.Equal(task.TotalCostUSD) {
		t.Errorf("precision lost: expected %s, got %s", task.TotalCostUSD, got.TotalCostUSD)
	}
}

// TestSQLiteBuffer_ConcurrentInsertNoBusy is the DEX-260 regression: under a
// fan-out write workload the buffer must not drop events to SQLITE_BUSY. The
// DEX-251 run produced ~163 silent drops of external_cost + retry_marker
// events because the WAL/synchronous pragmas were issued via db.Exec after
// Open, which only applies them to one pooled connection — and busy_timeout
// was missing entirely. With pragmas baked into the DSN (so they apply per
// connection) and busy_timeout set, every InsertEvent must succeed even when
// 50 goroutines hammer the buffer in parallel.
//
// goroutines × eventsPerGoroutine = 1000 inserts. On the broken code this
// triggered SQLITE_BUSY within the first ~50 inserts; on the fixed code it
// completes in <1 s with zero errors.
func TestSQLiteBuffer_ConcurrentInsertNoBusy(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	parent := core.NewTask("concurrent_writer")
	if err := buf.InsertTask(parent); err != nil {
		t.Fatalf("insert parent task: %v", err)
	}
	taskID := parent.TaskID

	const (
		goroutines        = 50
		eventsPerRoutine  = 20
		expectedTotal     = goroutines * eventsPerRoutine
	)

	var (
		wg       sync.WaitGroup
		failures atomic.Int64
	)

	wg.Add(goroutines)
	for g := 0; g < goroutines; g++ {
		go func(workerID int) {
			defer wg.Done()
			for i := 0; i < eventsPerRoutine; i++ {
				ev := core.NewEvent(taskID, core.EventTypeExternalCost)
				ev.ServiceName = "concurrent-test"
				ev.CostUSD = decimal.NewFromFloat(0.0001)
				ev.Details["worker"] = workerID
				ev.Details["i"] = i
				if err := buf.InsertEvent(ev); err != nil {
					failures.Add(1)
					t.Errorf("worker %d insert %d: %v", workerID, i, err)
				}
			}
		}(g)
	}
	wg.Wait()

	if got := failures.Load(); got != 0 {
		t.Fatalf("expected zero insert failures, got %d", got)
	}

	events, err := buf.QueryEvents(taskID.String())
	if err != nil {
		t.Fatalf("query events: %v", err)
	}
	if len(events) != expectedTotal {
		t.Fatalf("expected %d events persisted, got %d (silent drops?)",
			expectedTotal, len(events))
	}
}
