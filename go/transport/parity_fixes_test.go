package transport

import (
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/security"
)

// TestSQLiteBuffer_TaskMetadataPersisted is the regression test for the 🔴 bug
// where InsertTask / InsertTaskWithEvents hardcoded the metadata column to
// "{}" — task metadata (incl. _trace_links, session flags) was never stored.
func TestSQLiteBuffer_TaskMetadataPersisted(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("resolve_ticket")
	task.Metadata["session"] = true
	task.Metadata["_trace_links"] = []interface{}{
		map[string]interface{}{"provider": "langsmith", "trace_id": "abc"},
	}
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert failed: %v", err)
	}

	// Round-trips via GetTask.
	got, err := buf.GetTask(task.TaskID.String())
	if err != nil || got == nil {
		t.Fatalf("get failed: %v", err)
	}
	if got.Metadata["session"] != true {
		t.Errorf("task metadata dropped: expected session=true, got %v", got.Metadata["session"])
	}
	if _, ok := got.Metadata["_trace_links"]; !ok {
		t.Error("_trace_links did not survive the InsertTask -> GetTask round-trip")
	}

	// Round-trips via QueryTasksByIDs (the pusher's task-sync path).
	tasks, err := buf.QueryTasksByIDs([]string{task.TaskID.String()})
	if err != nil || len(tasks) != 1 {
		t.Fatalf("QueryTasksByIDs: err=%v len=%d", err, len(tasks))
	}
	if tasks[0].Metadata["session"] != true {
		t.Error("QueryTasksByIDs dropped task metadata")
	}

	// UpdateTask persists metadata mutations (e.g. trace links added mid-task).
	got.Metadata["agent"] = "researcher"
	if err := buf.UpdateTask(*got); err != nil {
		t.Fatalf("update failed: %v", err)
	}
	after, _ := buf.GetTask(task.TaskID.String())
	if after.Metadata["agent"] != "researcher" {
		t.Error("UpdateTask did not persist a task metadata mutation")
	}
}

// TestSQLiteBuffer_PurgeOldPendingEvents verifies stale pending events are
// purged while fresh pending events are kept — so a permanently failing sync
// no longer grows the buffer unbounded.
func TestSQLiteBuffer_PurgeOldPendingEvents(t *testing.T) {
	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer buf.Close()

	task := core.NewTask("test")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("insert task: %v", err)
	}

	oldEvent := core.NewEvent(task.TaskID, core.EventTypeExternalCost)
	oldEvent.OccurredAt = time.Now().UTC().Add(-30 * 24 * time.Hour)
	freshEvent := core.NewEvent(task.TaskID, core.EventTypeExternalCost)
	if err := buf.InsertEvent(oldEvent); err != nil {
		t.Fatalf("insert old event: %v", err)
	}
	if err := buf.InsertEvent(freshEvent); err != nil {
		t.Fatalf("insert fresh event: %v", err)
	}

	n, err := buf.PurgeOldPendingEvents(time.Now().UTC().Add(-7 * 24 * time.Hour))
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 1 {
		t.Errorf("expected 1 stale pending event purged, got %d", n)
	}
	remaining, _ := buf.QueryEvents(task.TaskID.String())
	if len(remaining) != 1 {
		t.Errorf("expected 1 event remaining (the fresh one), got %d", len(remaining))
	}
}

// TestEventPusher_RedactTaskMetadata verifies task metadata and attribution
// fields are redacted/hashed before a task is serialized for push — the PII
// fix for task-level data (previously only event.details was protected).
func TestEventPusher_RedactTaskMetadata(t *testing.T) {
	p := &EventPusher{
		redactFields:   []string{"ssn"},
		hashCustomerID: true,
	}

	task := core.NewTask("t")
	task.CustomerID = "acme"
	task.ProjectID = "proj"
	task.Metadata["ssn"] = "123-45-6789"
	task.Metadata["safe"] = "ok"

	tasks := []core.Task{task}
	p.redactTaskMetadata(tasks)

	if tasks[0].Metadata["ssn"] == "123-45-6789" {
		t.Error("raw ssn leaked in task metadata — not redacted before push")
	}
	if tasks[0].Metadata["safe"] != "ok" {
		t.Error("non-sensitive task metadata should be preserved")
	}
	if tasks[0].CustomerID == "acme" {
		t.Error("customer_id should be hashed when hash_customer_id is set")
	}
	if tasks[0].CustomerID != security.HashValue("acme") {
		t.Errorf("customer_id should be the SHA-256 hash of 'acme'")
	}
	if tasks[0].ProjectID == "proj" {
		t.Error("project_id should be hashed when hash_customer_id is set")
	}
}
