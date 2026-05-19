//! Trace linking helpers for external observability platforms.
//!
//! Mirrors the Python `dexcost.integrations.traces` module. From inside an
//! active task context (i.e. a future running under
//! [`crate::core::context::with_task`]) callers can link an external trace
//! identifier (Langfuse, LangSmith, OpenTelemetry trace, …) to the current
//! task. Trace links are stored in a global, lock-protected table keyed by
//! `task_id` so they survive the [`get_current_task`] clone.
//!
//! # Example
//!
//! ```no_run
//! use dexcost::integrations::traces::{link_trace, get_trace_links};
//!
//! # async fn _example() -> Result<(), Box<dyn std::error::Error>> {
//! // Inside an active task context...
//! link_trace("langfuse", "trace-abc-123")?;
//! let links = get_trace_links();
//! assert_eq!(links.len(), 1);
//! # Ok(())
//! # }
//! ```

use std::collections::HashMap;
use std::sync::LazyLock;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::core::context::get_current_task;
use crate::error::DexcostError;

/// A single external-trace association recorded against a task.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TraceLink {
    /// Name of the observability platform (e.g. `"langfuse"`, `"langsmith"`,
    /// `"otel"`).
    pub provider: String,
    /// The trace or run identifier from the external platform.
    pub trace_id: String,
}

/// Process-wide table mapping `task_id` to a list of recorded trace links.
///
/// Rust `Task`s pulled out of `tokio::task_local!` storage are clones, so a
/// mutation on the clone does not propagate back. Instead we keep the
/// mapping out-of-band and let consumers query by `task_id`.
static TRACE_LINKS: LazyLock<Mutex<HashMap<String, Vec<TraceLink>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn lock_links() -> std::sync::MutexGuard<'static, HashMap<String, Vec<TraceLink>>> {
    TRACE_LINKS.lock().unwrap_or_else(|e| {
        eprintln!("[dexcost] trace-link mutex poisoned, recovering: {}", e);
        e.into_inner()
    })
}

/// Associate an external trace with the **current** active dexcost task.
///
/// Returns `Err(DexcostError::NotInitialized)` (mirroring Python's
/// `RuntimeError`) when no task is in scope. The function is cheap — it does
/// not allocate beyond the new entry.
///
/// # Errors
///
/// * Returns [`DexcostError::NotInitialized`] when called outside an active
///   task context.
pub fn link_trace(provider: &str, trace_id: &str) -> Result<(), DexcostError> {
    let task = get_current_task().ok_or(DexcostError::NotInitialized)?;
    let mut guard = lock_links();
    let entry = guard.entry(task.task_id.clone()).or_default();
    entry.push(TraceLink {
        provider: provider.to_string(),
        trace_id: trace_id.to_string(),
    });
    Ok(())
}

/// Return all trace links for the current active task.
///
/// Returns an empty vector when no task is in scope. Mirrors the Python
/// `get_trace_links()` helper which never raises.
pub fn get_trace_links() -> Vec<TraceLink> {
    let task = match get_current_task() {
        Some(t) => t,
        None => return Vec::new(),
    };
    let guard = lock_links();
    guard.get(&task.task_id).cloned().unwrap_or_default()
}

/// Return all recorded trace links for a specific `task_id`.
///
/// Useful when integrators need to look up the trace links of a task that is
/// no longer in scope (for example when serialising at flush time).
pub fn get_trace_links_for_task(task_id: &str) -> Vec<TraceLink> {
    lock_links().get(task_id).cloned().unwrap_or_default()
}

/// Remove all recorded trace links for a `task_id`. Should be called when the
/// task is finalised so the global table does not grow without bound.
pub fn clear_trace_links_for_task(task_id: &str) {
    lock_links().remove(task_id);
}

/// Clear every recorded trace link. Intended for tests.
pub fn clear_all_trace_links() {
    lock_links().clear();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::context::with_task;
    use crate::core::models::Task;

    /// Process-wide lock. The `TRACE_LINKS` table is global, so tests need to
    /// run sequentially even though `cargo test` defaults to parallel.
    static TEST_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

    fn test_lock() -> std::sync::MutexGuard<'static, ()> {
        TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    // The process-wide `test_lock` guard is intentionally held across the
    // `with_task(...).await` below: it serialises access to the global
    // `TRACE_LINKS` table for the whole test body. Dropping it earlier would
    // defeat that purpose, so the lint is allowed here.
    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn link_trace_inside_active_task() {
        let _g = test_lock();
        clear_all_trace_links();

        let task = Task::new("trace_test");
        let task_id = task.task_id.clone();

        with_task(task, async {
            link_trace("langfuse", "trace-abc-123").expect("link inside task");
            link_trace("langsmith", "run-def-456").expect("second link");

            let links = get_trace_links();
            assert_eq!(links.len(), 2);
            assert_eq!(links[0].provider, "langfuse");
            assert_eq!(links[0].trace_id, "trace-abc-123");
            assert_eq!(links[1].provider, "langsmith");
        })
        .await;

        // After the scope exits, get_trace_links() returns empty (no current task)
        // but the per-task list is still queryable by id.
        assert!(get_trace_links().is_empty());
        let by_id = get_trace_links_for_task(&task_id);
        assert_eq!(by_id.len(), 2);

        clear_trace_links_for_task(&task_id);
        assert!(get_trace_links_for_task(&task_id).is_empty());
    }

    #[tokio::test]
    async fn link_trace_outside_active_task_errors() {
        let _g = test_lock();
        clear_all_trace_links();

        let result = link_trace("langfuse", "trace-1");
        assert!(matches!(result, Err(DexcostError::NotInitialized)));
        assert!(get_trace_links().is_empty());
    }

    #[test]
    fn trace_link_round_trips_json() {
        let link = TraceLink {
            provider: "otel".to_string(),
            trace_id: "abcd".to_string(),
        };
        let s = serde_json::to_string(&link).unwrap();
        let parsed: TraceLink = serde_json::from_str(&s).unwrap();
        assert_eq!(parsed, link);
    }
}
