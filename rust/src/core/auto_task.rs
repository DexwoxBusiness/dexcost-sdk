//! Auto-task creation and finalization for instrumented calls without explicit
//! task context.
//!
//! When a wrapper client records an LLM call and no explicit task is active,
//! an auto-task is created to group the cost event. Once the call completes,
//! the auto-task is finalized with aggregated costs.

use chrono::Utc;

use crate::core::context::get_dexcost_context;
use crate::core::models::{CostEvent, EventType, Task, TaskStatus};
use crate::transport::buffer::EventBuffer;

/// Creates an auto-task for instrumented calls without explicit task context.
///
/// Reads `customer_id` and `project_id` from the ambient
/// [`DexcostContext`](crate::DexcostContext) if available. Sets metadata
/// indicating this is a session-initiated auto-task.
///
/// # Arguments
///
/// * `task_type` - The type/name for this auto-task (e.g. `"openai.chat"`).
pub async fn create_auto_task(task_type: &str) -> Task {
    let ctx = get_dexcost_context().await;
    let mut task = Task::new(task_type);

    if let Some(c) = ctx {
        task.customer_id = c.customer_id;
        task.project_id = c.project_id;
    }

    task.metadata
        .insert("session".to_string(), serde_json::Value::Bool(true));
    task.metadata.insert(
        "initiated_by".to_string(),
        serde_json::Value::String(task_type.to_string()),
    );

    task
}

/// Finalizes an auto-task: aggregates event costs, sets status, persists to buffer.
///
/// If an event is provided, its cost and token fields are aggregated into the
/// task totals. Retry metrics are updated when the event has `is_retry` set.
///
/// # Arguments
///
/// * `task` - Mutable reference to the task being finalized.
/// * `event` - Optional cost event to aggregate into the task.
/// * `status` - The final status string (`"success"` or `"failed"`).
/// * `buffer` - The event buffer used to persist the updated task.
pub fn finalize_auto_task(
    task: &mut Task,
    event: Option<&CostEvent>,
    status: &str,
    buffer: &mut EventBuffer,
) {
    task.status = match status {
        "success" => TaskStatus::Success,
        "failed" => TaskStatus::Failed,
        _ => TaskStatus::Pending,
    };
    task.ended_at = Some(Utc::now());

    if let Some(ev) = event {
        match ev.event_type {
            EventType::LlmCall => task.llm_cost_usd += ev.cost_usd,
            EventType::ExternalCost => task.external_cost_usd += ev.cost_usd,
            EventType::ComputeCost => task.compute_cost_usd += ev.cost_usd,
            EventType::RetryMarker => { /* retry markers don't add to category costs */ }
        }
        task.total_cost_usd = task.llm_cost_usd + task.external_cost_usd + task.compute_cost_usd;

        if let Some(tokens) = ev.input_tokens {
            task.total_input_tokens += tokens;
        }
        if let Some(tokens) = ev.output_tokens {
            task.total_output_tokens += tokens;
        }
        if let Some(tokens) = ev.cached_tokens {
            task.total_cached_tokens += tokens;
        }
        if ev.is_retry {
            task.retry_count += 1;
            task.retry_cost_usd += ev.cost_usd;
        }
    }

    buffer.upsert_task(task.clone());
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::context::{clear_dexcost_context, set_context, DexcostContext};
    use crate::core::models::{CostEvent, EventType, TaskStatus};
    use crate::transport::buffer::EventBuffer;
    use rust_decimal::Decimal;

    // Process-wide serialisation lock — the global context is shared.
    use std::sync::LazyLock;
    use tokio::sync::Mutex;

    static CTX_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

    #[tokio::test]
    async fn test_create_auto_task_with_context() {
        let _g = CTX_LOCK.lock().await;

        set_context(DexcostContext {
            customer_id: Some("auto-cust".into()),
            project_id: Some("auto-proj".into()),
            metadata: None,
            agent: None,
        })
        .await;

        let task = create_auto_task("openai.chat").await;
        assert_eq!(task.task_type, "openai.chat");
        assert_eq!(task.customer_id.as_deref(), Some("auto-cust"));
        assert_eq!(task.project_id.as_deref(), Some("auto-proj"));
        assert_eq!(task.status, TaskStatus::Pending);
        assert_eq!(
            task.metadata.get("session"),
            Some(&serde_json::Value::Bool(true))
        );
        assert_eq!(
            task.metadata.get("initiated_by"),
            Some(&serde_json::Value::String("openai.chat".to_string()))
        );

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

    #[tokio::test]
    async fn test_finalize_auto_task_with_llm_event() {
        let _g = CTX_LOCK.lock().await;
        clear_dexcost_context().await;

        let mut task = create_auto_task("openai.chat").await;
        let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
        event.cost_usd = Decimal::new(5, 2); // 0.05
        event.input_tokens = Some(1000);
        event.output_tokens = Some(500);

        let mut buffer = EventBuffer::new().unwrap();
        finalize_auto_task(&mut task, Some(&event), "success", &mut buffer);

        assert_eq!(task.status, TaskStatus::Success);
        assert!(task.ended_at.is_some());
        assert_eq!(task.llm_cost_usd, Decimal::new(5, 2));
        assert_eq!(task.total_cost_usd, Decimal::new(5, 2));
        assert_eq!(task.total_input_tokens, 1000);
        assert_eq!(task.total_output_tokens, 500);
        assert_eq!(task.retry_count, 0);

        // Task should be persisted in buffer.
        assert_eq!(buffer.task_count(), 1);
    }

    #[tokio::test]
    async fn test_finalize_auto_task_with_external_cost() {
        let _g = CTX_LOCK.lock().await;
        clear_dexcost_context().await;

        let mut task = create_auto_task("google_maps").await;
        let mut event = CostEvent::new(&task.task_id, EventType::ExternalCost);
        event.cost_usd = Decimal::new(1, 2); // 0.01

        let mut buffer = EventBuffer::new().unwrap();
        finalize_auto_task(&mut task, Some(&event), "success", &mut buffer);

        assert_eq!(task.external_cost_usd, Decimal::new(1, 2));
        assert_eq!(task.total_cost_usd, Decimal::new(1, 2));
    }

    #[tokio::test]
    async fn test_finalize_auto_task_with_retry_event() {
        let _g = CTX_LOCK.lock().await;
        clear_dexcost_context().await;

        let mut task = create_auto_task("openai.chat").await;
        let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
        event.cost_usd = Decimal::new(3, 2); // 0.03
        event.is_retry = true;
        event.retry_reason = Some("rate_limit".to_string());
        event.input_tokens = Some(500);
        event.output_tokens = Some(200);

        let mut buffer = EventBuffer::new().unwrap();
        finalize_auto_task(&mut task, Some(&event), "success", &mut buffer);

        assert_eq!(task.retry_count, 1);
        assert_eq!(task.retry_cost_usd, Decimal::new(3, 2));
        assert_eq!(task.llm_cost_usd, Decimal::new(3, 2));
    }

    #[tokio::test]
    async fn test_finalize_auto_task_without_event() {
        let _g = CTX_LOCK.lock().await;
        clear_dexcost_context().await;

        let mut task = create_auto_task("noop").await;

        let mut buffer = EventBuffer::new().unwrap();
        finalize_auto_task(&mut task, None, "failed", &mut buffer);

        assert_eq!(task.status, TaskStatus::Failed);
        assert!(task.ended_at.is_some());
        assert_eq!(task.total_cost_usd, Decimal::ZERO);
    }

    #[tokio::test]
    async fn test_finalize_auto_task_compute_cost() {
        let _g = CTX_LOCK.lock().await;
        clear_dexcost_context().await;

        let mut task = create_auto_task("gpu_inference").await;
        let mut event = CostEvent::new(&task.task_id, EventType::ComputeCost);
        event.cost_usd = Decimal::new(10, 2); // 0.10

        let mut buffer = EventBuffer::new().unwrap();
        finalize_auto_task(&mut task, Some(&event), "success", &mut buffer);

        assert_eq!(task.compute_cost_usd, Decimal::new(10, 2));
        assert_eq!(task.total_cost_usd, Decimal::new(10, 2));
    }
}
