//! Development mode console output for dexcost.
//!
//! When `DEXCOST_ENV=development` or `environment = "development"` is passed
//! to `init()`, every recorded event is printed to stderr with a formatted
//! summary.

use std::sync::atomic::{AtomicBool, Ordering};

use rust_decimal::Decimal;

use crate::core::models::{CostConfidence, CostEvent, EventType, Task};

/// Module-level flag — `true` when dev mode is active.
static DEV_MODE: AtomicBool = AtomicBool::new(false);

/// Returns `true` if development mode is enabled.
pub fn is_dev_mode() -> bool {
    DEV_MODE.load(Ordering::Relaxed)
}

/// Enables development mode and prints a banner to stderr.
pub fn enable_dev_mode() {
    DEV_MODE.store(true, Ordering::Relaxed);
    print_line("\x1b[36m[dexcost]\x1b[0m dev mode \u{2014} cloud sync disabled");
}

/// Print a single event to stderr (no-op when dev mode is off).
pub fn log_event(event: &CostEvent, task_type: &str) {
    if !is_dev_mode() {
        return;
    }

    let cost = &event.cost_usd;

    match event.event_type {
        EventType::LlmCall => {
            let provider = event.provider.as_deref().unwrap_or("?");
            let model = event.model.as_deref().unwrap_or("?");
            let in_tok = event.input_tokens.unwrap_or(0);
            let out_tok = event.output_tokens.unwrap_or(0);
            let cached = event.cached_tokens.unwrap_or(0);

            let retry_tag = if event.is_retry {
                "  \x1b[33m(retry)\x1b[0m"
            } else {
                ""
            };
            let cache_tag = if cached > 0 {
                format!("  cached: {}", cached)
            } else {
                String::new()
            };

            print_line(&format!(
                "\x1b[32m\u{2713}\x1b[0m llm_call  {}/{}  {} in / {} out{}  ${}{}{}",
                provider,
                model,
                in_tok,
                out_tok,
                cache_tag,
                cost,
                retry_tag,
                task_tag(task_type),
            ));
        }
        EventType::ExternalCost | EventType::ComputeCost => {
            let service = event.service_name.as_deref().unwrap_or("unknown");
            let event_type_str = match event.event_type {
                EventType::ExternalCost => "external_cost",
                EventType::ComputeCost => "compute_cost",
                _ => "external_cost",
            };

            if event.cost_confidence == CostConfidence::Unknown || *cost == Decimal::ZERO {
                print_line(&format!(
                    "\x1b[33m\u{26a0}\x1b[0m {}  {}  $0.00 \x1b[33m(no rate configured)\x1b[0m{}",
                    event_type_str,
                    service,
                    task_tag(task_type),
                ));
            } else {
                print_line(&format!(
                    "\x1b[32m\u{2713}\x1b[0m {}  {}  ${}{}",
                    event_type_str,
                    service,
                    cost,
                    task_tag(task_type),
                ));
            }
        }
        EventType::RetryMarker => {
            let reason = event.retry_reason.as_deref().unwrap_or("unknown");
            print_line(&format!(
                "\x1b[33m\u{21bb}\x1b[0m retry_marker  reason: {}  ${}{}",
                reason,
                cost,
                task_tag(task_type),
            ));
        }
        EventType::Network => {
            let host = event
                .details
                .get("host")
                .and_then(|v| v.as_str())
                .unwrap_or("?");
            print_line(&format!(
                "\x1b[36m\u{2192}\x1b[0m network  {}  ${}{}",
                host,
                cost,
                task_tag(task_type),
            ));
        }
    }
}

/// Print task completion summary to stderr (no-op when dev mode is off).
pub fn log_task_complete(task: &Task) {
    if !is_dev_mode() {
        return;
    }

    let status = match task.status {
        crate::core::models::TaskStatus::Pending => "pending",
        crate::core::models::TaskStatus::Success => "success",
        crate::core::models::TaskStatus::Failed => "failed",
        crate::core::models::TaskStatus::Running => "running",
    };

    let retry_info = if task.retry_count > 0 {
        format!(
            "  retries: {}  retry cost: ${}",
            task.retry_count, task.retry_cost_usd
        )
    } else {
        String::new()
    };

    print_line(&format!(
        "\x1b[36m\u{2713}\x1b[0m task {}  {}  total: ${}{}",
        status, task.task_type, task.total_cost_usd, retry_info,
    ));
}

/// Format a task type tag for appending to log lines.
fn task_tag(task_type: &str) -> String {
    if task_type.is_empty() {
        String::new()
    } else {
        format!("  \x1b[90m(task: {})\x1b[0m", task_type)
    }
}

/// Print a formatted line to stderr.
fn print_line(msg: &str) {
    eprintln!("\x1b[36m[dexcost]\x1b[0m {}", msg);
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reset dev mode after each test — since the AtomicBool is global we
    // cannot truly isolate tests, but we can at least leave it in a known
    // state.  These tests are mainly compile-checks and smoke tests.

    #[test]
    fn test_dev_mode_default_off() {
        // Depending on test ordering this may already be true, so we just
        // ensure the function does not panic.
        let _ = is_dev_mode();
    }

    #[test]
    fn test_log_event_noop_when_off() {
        // Should not panic even when dev mode is off.
        DEV_MODE.store(false, Ordering::Relaxed);
        let event = CostEvent::new("task-1", EventType::LlmCall);
        log_event(&event, "test_task");
    }

    #[test]
    fn test_log_task_complete_noop_when_off() {
        DEV_MODE.store(false, Ordering::Relaxed);
        let task = Task::new("test_task");
        log_task_complete(&task);
    }

    #[test]
    fn test_task_tag_empty() {
        assert_eq!(task_tag(""), String::new());
    }

    #[test]
    fn test_task_tag_present() {
        let tag = task_tag("resolve_ticket");
        assert!(tag.contains("resolve_ticket"));
    }
}
