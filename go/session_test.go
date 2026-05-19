package dexcost

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/transport"
)

func newTestSessionBuffer(t *testing.T) core.Buffer {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "session_test.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	t.Cleanup(func() { buf.Close() })
	return buf
}

func TestSessionManager_ReturnsExistingTask(t *testing.T) {
	sm := NewSessionManager(30 * time.Second)
	defer sm.Clear()

	task := core.NewTask("explicit_task")
	ctx := core.WithTask(context.Background(), &task)

	_, got := sm.GetOrCreateSession(ctx, "llm_call", nil)
	if got.TaskID != task.TaskID {
		t.Errorf("expected existing task %s, got %s", task.TaskID, got.TaskID)
	}
	if sm.ActiveSessionCount() != 0 {
		t.Errorf("expected 0 sessions when explicit task exists, got %d", sm.ActiveSessionCount())
	}
}

func TestSessionManager_CreatesSessionTask(t *testing.T) {
	sm := NewSessionManager(30 * time.Second)
	defer sm.Clear()

	ctx := context.Background()
	ctx = core.SetContext(ctx, &core.ContextData{
		CustomerID: "acme",
		ProjectID:  "chatbot",
	})

	ctx, task := sm.GetOrCreateSession(ctx, "llm_call", nil)
	if task == nil {
		t.Fatal("expected non-nil session task")
	}
	if task.CustomerID != "acme" {
		t.Errorf("expected customer=acme, got %s", task.CustomerID)
	}
	if task.ProjectID != "chatbot" {
		t.Errorf("expected project=chatbot, got %s", task.ProjectID)
	}
	if task.Metadata["session"] != true {
		t.Error("expected metadata session=true")
	}
	if task.Metadata["initiated_by"] != "llm_call" {
		t.Errorf("expected metadata initiated_by=llm_call, got %v", task.Metadata["initiated_by"])
	}
	if sm.ActiveSessionCount() != 1 {
		t.Errorf("expected 1 active session, got %d", sm.ActiveSessionCount())
	}

	// Using the returned context should reuse the same session.
	_, task2 := sm.GetOrCreateSession(ctx, "http_call", nil)
	if task2.TaskID != task.TaskID {
		t.Errorf("expected same session task, got different: %s vs %s", task.TaskID, task2.TaskID)
	}
	if sm.ActiveSessionCount() != 1 {
		t.Errorf("expected still 1 active session after reuse, got %d", sm.ActiveSessionCount())
	}
}

func TestSessionManager_PersistsToBuffer(t *testing.T) {
	buf := newTestSessionBuffer(t)
	sm := NewSessionManager(30 * time.Second)
	defer sm.Clear()

	ctx := context.Background()
	_, task := sm.GetOrCreateSession(ctx, "llm_call", buf)

	// Verify task was stored.
	stored, err := buf.GetTask(task.TaskID.String())
	if err != nil {
		t.Fatalf("GetTask: %v", err)
	}
	if stored == nil {
		t.Fatal("expected session task to be persisted in buffer")
	}
	if stored.TaskType != "agent_session" {
		t.Errorf("expected task_type=agent_session, got %s", stored.TaskType)
	}
}

func TestSessionManager_FinalizeIdleSessions(t *testing.T) {
	sm := NewSessionManager(10 * time.Millisecond)
	defer sm.Clear()

	ctx := context.Background()
	sm.GetOrCreateSession(ctx, "llm_call", nil)
	if sm.ActiveSessionCount() != 1 {
		t.Fatalf("expected 1 session, got %d", sm.ActiveSessionCount())
	}

	// Wait for idle timeout.
	time.Sleep(20 * time.Millisecond)

	finalized := sm.FinalizeIdleSessions(nil)
	if len(finalized) != 1 {
		t.Fatalf("expected 1 finalized session, got %d", len(finalized))
	}
	if finalized[0].Status != core.TaskStatusSuccess {
		t.Errorf("expected status=success, got %s", finalized[0].Status)
	}
	if finalized[0].EndedAt == nil {
		t.Error("expected ended_at to be set")
	}
	if sm.ActiveSessionCount() != 0 {
		t.Errorf("expected 0 sessions after finalization, got %d", sm.ActiveSessionCount())
	}
}

func TestSessionManager_FinalizeIdleSessions_WithBuffer(t *testing.T) {
	buf := newTestSessionBuffer(t)
	sm := NewSessionManager(10 * time.Millisecond)
	defer sm.Clear()

	ctx := context.Background()
	_, task := sm.GetOrCreateSession(ctx, "llm_call", buf)

	time.Sleep(20 * time.Millisecond)
	sm.FinalizeIdleSessions(buf)

	// Verify task was updated in buffer.
	stored, err := buf.GetTask(task.TaskID.String())
	if err != nil {
		t.Fatalf("GetTask: %v", err)
	}
	if stored == nil {
		t.Fatal("expected task in buffer")
	}
	if stored.Status != core.TaskStatusSuccess {
		t.Errorf("expected status=success in buffer, got %s", stored.Status)
	}
	if stored.EndedAt == nil {
		t.Error("expected ended_at to be set in buffer")
	}
}

func TestSessionManager_ActiveDoesNotFinalize(t *testing.T) {
	sm := NewSessionManager(1 * time.Second)
	defer sm.Clear()

	ctx := context.Background()
	sm.GetOrCreateSession(ctx, "llm_call", nil)

	// Do not wait; session is not idle yet.
	finalized := sm.FinalizeIdleSessions(nil)
	if len(finalized) != 0 {
		t.Errorf("expected 0 finalized sessions, got %d", len(finalized))
	}
	if sm.ActiveSessionCount() != 1 {
		t.Errorf("expected 1 active session, got %d", sm.ActiveSessionCount())
	}
}

func TestGlobalSessionManager(t *testing.T) {
	resetSessionManager()
	defer resetSessionManager()

	sm := SessionMgr()
	if sm == nil {
		t.Fatal("expected non-nil global session manager")
	}
	sm2 := SessionMgr()
	if sm != sm2 {
		t.Error("expected same global session manager instance")
	}
}
