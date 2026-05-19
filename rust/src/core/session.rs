//! Session-based auto-grouping for dexcost.
//!
//! Groups related LLM and HTTP calls into a single task without requiring
//! explicit task wrappers. Uses a thread-safe `SessionManager` that tracks
//! sessions by thread/context ID with idle timeout finalization.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use chrono::Utc;

use crate::core::context::{get_current_task, get_dexcost_context_sync};
use crate::core::models::{Task, TaskStatus};

/// Entry in the session map — holds a task and the last activity timestamp.
struct SessionEntry {
    task: Task,
    last_activity: Instant,
}

/// Manages auto-created session tasks for grouping cost events.
///
/// When no explicit task is active, the session manager creates a session task
/// and returns it so that subsequent LLM and HTTP calls are grouped together.
pub struct SessionManager {
    sessions: Mutex<HashMap<u64, SessionEntry>>,
    idle_timeout: Duration,
}

impl SessionManager {
    /// Creates a new `SessionManager` with the given idle timeout.
    ///
    /// Sessions that have had no activity for longer than `idle_timeout` will
    /// be finalized by [`finalize_idle_sessions`](Self::finalize_idle_sessions).
    pub fn new(idle_timeout: Duration) -> Self {
        Self {
            sessions: Mutex::new(HashMap::new()),
            idle_timeout,
        }
    }

    /// Returns the active task from context, or creates a new session task.
    ///
    /// If an explicit task is already active in the current context (via
    /// task-local storage), that task is returned unchanged. Otherwise, a
    /// session task is created (or reused) for the current thread.
    ///
    /// # Arguments
    ///
    /// * `call_type` - Description of the call (e.g. `"llm_call"`, `"http_call"`).
    ///   Used as the `task_type` when creating a new session task if no ambient
    ///   context provides an agent name.
    pub fn get_or_create_session(&self, call_type: &str) -> Task {
        // If an explicit task is already active, use it and refresh activity.
        if let Some(existing) = get_current_task() {
            let ctx_id = Self::context_id();
            let mut sessions = self.sessions.lock().unwrap_or_else(|e| {
                eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
                e.into_inner()
            });
            if let Some(entry) = sessions.get_mut(&ctx_id) {
                entry.last_activity = Instant::now();
            }
            return existing;
        }

        let ctx_id = Self::context_id();
        let mut sessions = self.sessions.lock().unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        });

        // Reuse existing session for this context.
        if let Some(entry) = sessions.get_mut(&ctx_id) {
            entry.last_activity = Instant::now();
            return entry.task.clone();
        }

        // Create a new session task.
        let task_type = if call_type.is_empty() {
            "agent_session".to_string()
        } else {
            call_type.to_string()
        };

        let mut task = Task::new(&task_type);
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(call_type.to_string()),
        );

        // Try to pull customer_id / project_id from ambient context.
        // We avoid tokio::task::block_in_place here because it can deadlock
        // if the tokio runtime is single-threaded. Instead, use try_read()
        // on the underlying RwLock (non-blocking, best-effort).
        // The ambient context is a nice-to-have, not critical.
        if let Some(ctx) = get_dexcost_context_sync() {
            task.customer_id = ctx.customer_id;
            task.project_id = ctx.project_id;
        }

        sessions.insert(
            ctx_id,
            SessionEntry {
                task: task.clone(),
                last_activity: Instant::now(),
            },
        );

        task
    }

    /// Closes sessions that have been idle for longer than the configured timeout.
    ///
    /// Returns the list of finalized session tasks with status set to `Success`
    /// and `ended_at` set to the current time.
    pub fn finalize_idle_sessions(&self) -> Vec<Task> {
        let now = Instant::now();
        let mut finalized = Vec::new();

        let mut sessions = self.sessions.lock().unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        });
        let idle_ids: Vec<u64> = sessions
            .iter()
            .filter(|(_, entry)| now.duration_since(entry.last_activity) >= self.idle_timeout)
            .map(|(id, _)| *id)
            .collect();

        for ctx_id in idle_ids {
            if let Some(mut entry) = sessions.remove(&ctx_id) {
                entry.task.status = TaskStatus::Success;
                entry.task.ended_at = Some(Utc::now());
                finalized.push(entry.task);
            }
        }

        finalized
    }

    /// Returns the number of currently active sessions.
    pub fn active_session_count(&self) -> usize {
        let sessions = self.sessions.lock().unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        });
        sessions.len()
    }

    /// Removes all tracked sessions (primarily for testing).
    pub fn clear(&self) {
        let mut sessions = self.sessions.lock().unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        });
        sessions.clear();
    }

    /// Returns a unique context identifier for the current thread.
    fn context_id() -> u64 {
        // Use the raw thread ID as the context key.
        let id = std::thread::current().id();
        // Thread::id() -> ThreadId; use its Debug representation to extract a
        // stable u64. ThreadId(N) is guaranteed unique for the thread's lifetime.
        let debug = format!("{:?}", id);
        // Extract the numeric portion.
        debug
            .chars()
            .filter(|c| c.is_ascii_digit())
            .collect::<String>()
            .parse::<u64>()
            .unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// Module-level singleton
// ---------------------------------------------------------------------------

static SESSION_MANAGER: std::sync::OnceLock<SessionManager> = std::sync::OnceLock::new();

/// Returns the global session manager, creating it with a 30-second idle
/// timeout if it has not been initialized yet.
pub fn get_session_manager() -> &'static SessionManager {
    SESSION_MANAGER.get_or_init(|| SessionManager::new(Duration::from_secs(30)))
}

/// Resets the global session manager (for testing). Clears all sessions but
/// does not remove the singleton — it will be reused on the next call.
pub fn reset_session_manager() {
    if let Some(mgr) = SESSION_MANAGER.get() {
        mgr.clear();
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn test_new_session_created() {
        let mgr = SessionManager::new(Duration::from_secs(30));
        let task = mgr.get_or_create_session("llm_call");
        assert_eq!(task.task_type, "llm_call");
        assert_eq!(task.status, TaskStatus::Pending);
        assert!(!task.task_id.is_empty());
        assert_eq!(mgr.active_session_count(), 1);
    }

    #[test]
    fn test_reuses_existing_session() {
        let mgr = SessionManager::new(Duration::from_secs(30));
        let first = mgr.get_or_create_session("llm_call");
        let second = mgr.get_or_create_session("http_call");
        // Same thread -> same session reused.
        assert_eq!(first.task_id, second.task_id);
        assert_eq!(mgr.active_session_count(), 1);
    }

    #[test]
    fn test_session_metadata() {
        let mgr = SessionManager::new(Duration::from_secs(30));
        let task = mgr.get_or_create_session("llm_call");
        assert_eq!(
            task.metadata.get("session"),
            Some(&serde_json::Value::Bool(true))
        );
        assert_eq!(
            task.metadata.get("initiated_by"),
            Some(&serde_json::Value::String("llm_call".to_string()))
        );
    }

    #[test]
    fn test_finalize_idle_sessions() {
        let mgr = SessionManager::new(Duration::from_millis(1));
        let _task = mgr.get_or_create_session("llm_call");
        assert_eq!(mgr.active_session_count(), 1);

        // Wait for the session to become idle.
        std::thread::sleep(Duration::from_millis(10));

        let finalized = mgr.finalize_idle_sessions();
        assert_eq!(finalized.len(), 1);
        assert_eq!(finalized[0].status, TaskStatus::Success);
        assert!(finalized[0].ended_at.is_some());
        assert_eq!(mgr.active_session_count(), 0);
    }

    #[test]
    fn test_finalize_does_not_remove_active() {
        let mgr = SessionManager::new(Duration::from_secs(300));
        let _task = mgr.get_or_create_session("llm_call");

        let finalized = mgr.finalize_idle_sessions();
        assert!(finalized.is_empty());
        assert_eq!(mgr.active_session_count(), 1);
    }

    #[test]
    fn test_clear() {
        let mgr = SessionManager::new(Duration::from_secs(30));
        let _task = mgr.get_or_create_session("llm_call");
        assert_eq!(mgr.active_session_count(), 1);
        mgr.clear();
        assert_eq!(mgr.active_session_count(), 0);
    }

    #[test]
    fn test_empty_call_type_defaults_to_agent_session() {
        let mgr = SessionManager::new(Duration::from_secs(30));
        let task = mgr.get_or_create_session("");
        assert_eq!(task.task_type, "agent_session");
    }

    #[test]
    fn test_global_session_manager() {
        reset_session_manager();
        let mgr = get_session_manager();
        assert_eq!(mgr.active_session_count(), 0);
    }
}
