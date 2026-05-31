package dexcost

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// SessionManager auto-groups HTTP/LLM calls that happen outside an
// explicit task context into session tasks. It is goroutine-safe.
//
// NOTE: Single mutex is acceptable for typical agent workloads (< 100 concurrent tasks).
// For extreme throughput (> 1000 concurrent), consider sharded locking or sync.Map.
type SessionManager struct {
	mu          sync.Mutex
	sessions    map[uint64]*sessionEntry
	idleTimeout time.Duration
	nextID      uint64
}

// sessionEntry tracks a session task and the last time it was used.
type sessionEntry struct {
	task         *core.Task
	lastActivity time.Time
	// identity groups consecutive identity-keyed calls (customer|project|agent)
	// into one session task. Empty for context-ID-keyed sessions.
	identity string
}

// NewSessionManager creates a SessionManager with the given idle timeout.
// Sessions that have not been active for idleTimeout will be finalized by
// FinalizeIdleSessions.
func NewSessionManager(idleTimeout time.Duration) *SessionManager {
	return &SessionManager{
		sessions:    make(map[uint64]*sessionEntry),
		idleTimeout: idleTimeout,
	}
}

// sessionContextKey is the context key for storing a session ID.
type sessionContextKey struct{}

// GetOrCreateSession returns the active task from ctx, or creates a new
// session task if none exists. The session task is stored in the manager
// and a derived context with the session task is returned.
//
// callType describes the initiating call (e.g. "llm_call", "http_call")
// and is stored in the session task's metadata. buffer is used to persist
// the session task; it may be nil if persistence is not desired.
func (sm *SessionManager) GetOrCreateSession(ctx context.Context, callType string, buffer core.Buffer) (context.Context, *core.Task) {
	// If an explicit task is already active, return it as-is.
	if existing := core.GetCurrentTask(ctx); existing != nil {
		sm.mu.Lock()
		// Update activity for all sessions (keyed by context session ID if present).
		if sid, ok := ctx.Value(sessionContextKey{}).(uint64); ok {
			if entry, found := sm.sessions[sid]; found {
				entry.lastActivity = time.Now()
			}
		}
		sm.mu.Unlock()
		return ctx, existing
	}

	// Check if context already carries a session ID.
	if sid, ok := ctx.Value(sessionContextKey{}).(uint64); ok {
		sm.mu.Lock()
		entry, found := sm.sessions[sid]
		if found {
			entry.lastActivity = time.Now()
			task := entry.task
			sm.mu.Unlock()
			return core.WithTask(ctx, task), task
		}
		sm.mu.Unlock()
	}

	// Create a new session task.
	taskType := "agent_session"
	if cd := core.GetContextData(ctx); cd != nil && cd.Agent != "" {
		taskType = cd.Agent
	}
	task := core.CreateAutoTask(ctx, taskType)
	task.Metadata["session"] = true
	task.Metadata["initiated_by"] = callType

	if buffer != nil {
		if err := buffer.InsertTask(task); err != nil {
			log.Printf("[dexcost] failed to persist session task: %v", err)
		}
	}

	sm.mu.Lock()
	sm.nextID++
	sid := sm.nextID
	sm.sessions[sid] = &sessionEntry{
		task:         &task,
		lastActivity: time.Now(),
	}
	sm.mu.Unlock()

	ctx = context.WithValue(ctx, sessionContextKey{}, sid)
	ctx = core.WithTask(ctx, &task)
	return ctx, &task
}

// GetOrCreateSessionForIdentity returns a session task that groups consecutive
// anonymous calls sharing the same (customer_id, project_id, agent) identity
// derived from the ambient DexcostContext. The HTTP adapter uses this so that
// a burst of HTTP calls rolls up into one session task instead of creating a
// throwaway task per request (Python parity: adapters/http.py groups by thread;
// Go has no goroutine identity, so we group by attribution identity instead).
// Sessions are reused until FinalizeIdleSessions closes them.
func (sm *SessionManager) GetOrCreateSessionForIdentity(ctx context.Context, callType string, buffer core.Buffer) *core.Task {
	// An explicit task always wins.
	if existing := core.GetCurrentTask(ctx); existing != nil {
		return existing
	}

	var customerID, projectID, agent string
	if cd := core.GetContextData(ctx); cd != nil {
		customerID, projectID, agent = cd.CustomerID, cd.ProjectID, cd.Agent
	}
	identity := customerID + "\x00" + projectID + "\x00" + agent

	// Sprint 2 Theme D / §3.2.2 (B13): single locked find-or-create
	// critical section. Pre-fix the lookup released the mutex before
	// running CreateAutoTask + buffer.InsertTask, then re-acquired to
	// insert — concurrent callers with the same identity all missed
	// the lookup, all created tasks, all inserted under separate IDs
	// → duplicate sessions on customer dashboards.
	//
	// The buffer.InsertTask call still happens INSIDE the lock. That
	// is acceptable: SQLite INSERTs are sub-ms locally and only ONE
	// task is ever persisted per identity per process lifetime. For
	// the unbounded-throughput case the file-level comment at line 15
	// recommends sharded locking.
	taskType := "agent_session"
	if agent != "" {
		taskType = agent
	}

	sm.mu.Lock()
	defer sm.mu.Unlock()

	for _, entry := range sm.sessions {
		if entry.identity == identity {
			entry.lastActivity = time.Now()
			return entry.task
		}
	}

	task := core.CreateAutoTask(ctx, taskType)
	task.Metadata["session"] = true
	task.Metadata["initiated_by"] = callType

	if buffer != nil {
		if err := buffer.InsertTask(task); err != nil {
			log.Printf("[dexcost] failed to persist session task: %v", err)
		}
	}

	// Sprint 4 §5.2 (A3) — hard count cap. The idleTimeout reaper
	// already evicts time-stale sessions, but a flood of distinct
	// identities (e.g. one anonymous user per request in a misconfigured
	// app) could otherwise exhaust process memory faster than the
	// reaper runs. Cap at 10 000 active sessions: when over, evict the
	// least-recently-active entry to make room.
	const sessionsCap = 10_000
	if len(sm.sessions) >= sessionsCap {
		var oldestID uint64
		var oldestActivity time.Time
		first := true
		for id, e := range sm.sessions {
			if first || e.lastActivity.Before(oldestActivity) {
				oldestID = id
				oldestActivity = e.lastActivity
				first = false
			}
		}
		delete(sm.sessions, oldestID)
	}

	sm.nextID++
	sm.sessions[sm.nextID] = &sessionEntry{
		task:         &task,
		lastActivity: time.Now(),
		identity:     identity,
	}
	return &task
}

// FinalizeIdleSessions closes sessions that have been idle for at least
// the configured idleTimeout. Finalized tasks are set to "success" status
// with an ended_at timestamp. If buffer is non-nil, each finalized task
// is updated in storage.
func (sm *SessionManager) FinalizeIdleSessions(buffer core.Buffer) []core.Task {
	now := time.Now()
	var finalized []core.Task

	sm.mu.Lock()
	for sid, entry := range sm.sessions {
		if now.Sub(entry.lastActivity) >= sm.idleTimeout {
			entry.task.Status = core.TaskStatusSuccess
			ended := time.Now().UTC()
			entry.task.EndedAt = &ended
			finalized = append(finalized, *entry.task)
			delete(sm.sessions, sid)
		}
	}
	sm.mu.Unlock()

	if buffer != nil {
		for _, task := range finalized {
			if err := buffer.UpdateTask(task); err != nil {
				log.Printf("[dexcost] failed to update finalized session task: %v", err)
			}
		}
	}

	return finalized
}

// ActiveSessionCount returns the number of currently active sessions.
func (sm *SessionManager) ActiveSessionCount() int {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	return len(sm.sessions)
}

// Clear removes all tracked sessions. Intended for testing.
func (sm *SessionManager) Clear() {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	sm.sessions = make(map[uint64]*sessionEntry)
}

// globalSessionManager is the package-level SessionManager singleton.
var (
	globalSessionManager     *SessionManager
	globalSessionManagerOnce sync.Once
)

// SessionMgr returns the global SessionManager, creating it if needed.
// The default idle timeout is 30 seconds.
func SessionMgr() *SessionManager {
	globalSessionManagerOnce.Do(func() {
		globalSessionManager = NewSessionManager(30 * time.Second)
	})
	return globalSessionManager
}

// resetSessionManager resets the global session manager. Used in tests.
func resetSessionManager() {
	if globalSessionManager != nil {
		globalSessionManager.Clear()
	}
	globalSessionManager = nil
	globalSessionManagerOnce = sync.Once{}
}
