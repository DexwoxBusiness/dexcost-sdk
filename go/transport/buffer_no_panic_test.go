// Sprint 1 Theme B / §2.2.2 1c regression: GetTask must not panic when
// the SQLite cost columns contain a value that fails Decimal parsing
// (e.g. a partially-written row from a crash, or a forwards-incompat
// schema migration).
//
// Pre-fix: `decimal.RequireFromString` panics, taking down the SDK call
// (and on auto-task paths, the customer's request).
// Post-fix: corrupt columns log a warning and resolve to Decimal.Zero;
// the row is returned with the remaining fields populated.

package transport

import (
	"path/filepath"
	"testing"
)

func TestGetTask_CorruptCostStringDoesNotPanic(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("GetTask panicked on corrupt cost column: %v", r)
		}
	}()

	dbPath := filepath.Join(t.TempDir(), "test.db")
	buf, err := NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	defer buf.Close()

	taskID := "00000000-0000-0000-0000-000000000001"
	_, err = buf.db.Exec(`
		INSERT INTO tasks
		(task_id, task_type, status, started_at, metadata,
		 llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
		 total_input_tokens, total_output_tokens, total_cached_tokens,
		 retry_count, retry_cost_usd, failure_count,
		 schema_version, sync_status)
		VALUES (?, 'corrupt-test', 'completed', datetime('now'), '{}',
		        'not-a-number', '0', '0', '0',
		        0, 0, 0, 0, '0', 0,
		        'dexcost-v1', 'pending')
	`, taskID)
	if err != nil {
		t.Fatalf("seed insert failed: %v", err)
	}

	got, err := buf.GetTask(taskID)
	if err != nil {
		t.Fatalf("GetTask returned error on corrupt row (expected nil error + zeroed cost): %v", err)
	}
	if got == nil {
		t.Fatalf("GetTask returned nil task on corrupt row")
	}
	if !got.LLMCostUSD.IsZero() {
		t.Fatalf("expected LLMCostUSD=0 for corrupt column, got %s", got.LLMCostUSD)
	}
	if got.TaskType != "corrupt-test" {
		t.Fatalf("expected TaskType=corrupt-test, got %q", got.TaskType)
	}
}
