use std::collections::HashMap;
use std::sync::LazyLock;

use tokio::sync::RwLock;

use crate::core::models::{Task, TaskStatus};

tokio::task_local! {
    /// Task-local storage for the current tracked task.
    static CURRENT_TASK: Task;
}

/// Runs the given future with the task set in task-local storage.
/// This allows nested tasks to discover their parent via `get_current_task`.
pub async fn with_task<F, T>(task: Task, f: F) -> T
where
    F: std::future::Future<Output = T>,
{
    CURRENT_TASK.scope(task, f).await
}

/// Returns a clone of the current task from task-local storage, if any.
pub fn get_current_task() -> Option<Task> {
    CURRENT_TASK.try_with(|t| t.clone()).ok()
}

// ---------------------------------------------------------------------------
// Global ambient context — customer/project attribution without explicit IDs
// ---------------------------------------------------------------------------

/// Ambient attribution context that can be set once per request/operation.
/// Uses a module-level `RwLock` for cross-task visibility (unlike
/// `tokio::task_local!`, which is scoped to a single Tokio task).
#[derive(Clone, Debug, Default)]
pub struct DexcostContext {
    pub customer_id: Option<String>,
    pub project_id: Option<String>,
    pub metadata: Option<HashMap<String, serde_json::Value>>,
    /// Optional agent name. When set, it is used as the `task_type` for
    /// auto-created session tasks instead of the default. Mirrors Python's
    /// `ctx.agent` (`session.py:73-75`).
    pub agent: Option<String>,
}

static CURRENT_CONTEXT: LazyLock<RwLock<Option<DexcostContext>>> =
    LazyLock::new(|| RwLock::new(None));

/// Sets the ambient dexcost context (customer_id, project_id, metadata).
/// Overwrites any previously set context.
pub async fn set_context(ctx: DexcostContext) {
    let mut guard = CURRENT_CONTEXT.write().await;
    *guard = Some(ctx);
}

/// Returns a clone of the current ambient context, or `None` if not set.
pub async fn get_dexcost_context() -> Option<DexcostContext> {
    CURRENT_CONTEXT.read().await.clone()
}

/// Returns a clone of the current ambient context using a non-blocking try_read.
/// Returns `None` if the context is not set or the lock cannot be acquired.
/// This avoids potential deadlocks when called from synchronous code inside
/// an async runtime.
pub fn get_dexcost_context_sync() -> Option<DexcostContext> {
    match CURRENT_CONTEXT.try_read() {
        Ok(guard) => guard.clone(),
        Err(_) => {
            eprintln!("[dexcost] could not read ambient context (lock contention), skipping");
            None
        }
    }
}

/// Clears the ambient context. Should be called at the end of a request.
pub async fn clear_dexcost_context() {
    let mut guard = CURRENT_CONTEXT.write().await;
    *guard = None;
}

/// Creates a new `Task` from the ambient context plus the given `task_type`.
/// If no context is set, `customer_id` and `project_id` are empty strings.
///
/// When the ambient context carries an `agent` name, it overrides
/// `task_type` for the created (session) task — mirroring Python
/// `session.py:73-75`, where `ctx.agent` is used as the session `task_type`.
pub async fn create_auto_task(task_type: &str) -> Task {
    let ctx = get_dexcost_context().await;

    let effective_type = ctx
        .as_ref()
        .and_then(|c| c.agent.clone())
        .unwrap_or_else(|| task_type.to_string());

    let mut task = Task::new(&effective_type);
    task.status = TaskStatus::Pending;

    if let Some(c) = ctx {
        task.customer_id = c.customer_id;
        task.project_id = c.project_id;
        if let Some(meta) = c.metadata {
            task.metadata = meta;
        }
    }

    task
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // Process-wide serialisation lock — the global context is shared across
    // all tests in the process, so we must run them sequentially.
    use std::sync::LazyLock;
    use tokio::sync::Mutex;

    static CTX_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

    #[tokio::test]
    async fn test_set_and_get_context() {
        let _g = CTX_LOCK.lock().await;

        set_context(DexcostContext {
            customer_id: Some("acme".into()),
            project_id: Some("chatbot".into()),
            metadata: None,
            agent: None,
        })
        .await;

        let ctx = get_dexcost_context().await;
        assert!(ctx.is_some());
        let ctx = ctx.unwrap();
        assert_eq!(ctx.customer_id.as_deref(), Some("acme"));
        assert_eq!(ctx.project_id.as_deref(), Some("chatbot"));

        clear_dexcost_context().await;
    }

    #[tokio::test]
    async fn test_get_context_returns_none_when_not_set() {
        let _g = CTX_LOCK.lock().await;

        clear_dexcost_context().await;
        let ctx = get_dexcost_context().await;
        assert!(ctx.is_none());
    }

    #[tokio::test]
    async fn test_clear_context() {
        let _g = CTX_LOCK.lock().await;

        set_context(DexcostContext {
            customer_id: Some("test".into()),
            project_id: None,
            metadata: None,
            agent: None,
        })
        .await;
        clear_dexcost_context().await;
        let ctx = get_dexcost_context().await;
        assert!(ctx.is_none());
    }

    #[tokio::test]
    async fn test_create_auto_task_with_context() {
        let _g = CTX_LOCK.lock().await;

        set_context(DexcostContext {
            customer_id: Some("auto-customer".into()),
            project_id: Some("auto-project".into()),
            metadata: None,
            agent: None,
        })
        .await;

        let task = create_auto_task("openai.chat").await;
        assert_eq!(task.customer_id.as_deref(), Some("auto-customer"));
        assert_eq!(task.project_id.as_deref(), Some("auto-project"));
        assert_eq!(task.task_type, "openai.chat");
        assert_eq!(task.status, TaskStatus::Pending);
        assert!(!task.task_id.is_empty());

        clear_dexcost_context().await;
    }

    // Gap 9: ctx.agent overrides the task_type of the auto-created session task.
    #[tokio::test]
    async fn test_create_auto_task_uses_agent_as_task_type() {
        let _g = CTX_LOCK.lock().await;

        set_context(DexcostContext {
            customer_id: Some("acme".into()),
            project_id: None,
            metadata: None,
            agent: Some("support_bot".into()),
        })
        .await;

        // The passed task_type is overridden by ctx.agent.
        let task = create_auto_task("openai.chat").await;
        assert_eq!(task.task_type, "support_bot");
        assert_eq!(task.customer_id.as_deref(), Some("acme"));

        clear_dexcost_context().await;
    }

    #[tokio::test]
    async fn test_create_auto_task_without_context() {
        let _g = CTX_LOCK.lock().await;

        clear_dexcost_context().await;
        let task = create_auto_task("test.call").await;
        assert!(task.customer_id.is_none());
        assert!(task.project_id.is_none());
        assert_eq!(task.task_type, "test.call");
    }
}
