//! Dexcost Rust SDK — Agent Cost Capture Example
//!
//! Run with: cargo run --example agent_cost
//!
//! This example demonstrates wiring the dexcost SDK around a local AI agent:
//!   1. Records LLM call costs (provider: "local", model: "local-llm")
//!   2. Records non-LLM tool costs (web search, maps API)
//!   3. Demonstrates retry waste tracking (simulated rate-limit retry)
//!   4. Verifies all events appear in the buffer with correct schema fields.
//!
//! No API key required — runs in offline/dev mode.

use dexcost::core::models::{EventType, TaskStatus};
use dexcost::core::tracker::TaskOptions;
use dexcost::{close, flush, init, start_task, Config};
use rust_decimal::Decimal;

/// Simulates a local LLM call.
/// Returns (output_tokens, latency_ms, should_retry).
fn simulate_llm_call(prompt_tokens: i64) -> (i64, i64, bool) {
    // Simulate 3x token amplification (common for local models).
    let output_tokens = prompt_tokens * 3;
    // Simulate occasional rate-limit (10% chance).
    let should_retry = rand_u8() > 230;
    (output_tokens, 180, should_retry)
}

/// Simulates a tool call cost.
/// Returns (service_name, cost_usd, details_json).
fn simulate_tool_call(
    tool: &str,
) -> (
    &str,
    Decimal,
    std::collections::HashMap<String, serde_json::Value>,
) {
    match tool {
        "web_search" => {
            let mut details = std::collections::HashMap::new();
            details.insert(
                "query".to_string(),
                serde_json::Value::String("weather forecast".to_string()),
            );
            details.insert(
                "results_count".to_string(),
                serde_json::Value::Number(5.into()),
            );
            ("web_search", Decimal::new(2, 3), details) // $0.002
        }
        "maps_api" => {
            let mut details = std::collections::HashMap::new();
            details.insert(
                "operation".to_string(),
                serde_json::Value::String("route".to_string()),
            );
            details.insert("waypoints".to_string(), serde_json::Value::Number(3.into()));
            ("maps_api", Decimal::new(5, 3), details) // $0.005
        }
        _ => {
            let details = std::collections::HashMap::new();
            ("unknown", Decimal::ZERO, details)
        }
    }
}

/// Seeded from monotonic clock for deterministic-ish output in examples.
fn rand_u8() -> u8 {
    use std::time::Instant;
    let now = Instant::now();
    (now.elapsed().as_nanos() % 256) as u8
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("[dexcost] Initializing SDK (offline mode)...");
    init(Config::default())?;

    // ── Start a task for the agent run ──────────────────────────────────────
    let mut task = start_task(
        "local_agent_task",
        TaskOptions {
            customer_id: Some("demo-corp".into()),
            project_id: Some("agent-demo".into()),
            metadata: Some({
                let mut m = std::collections::HashMap::new();
                m.insert(
                    "agent_framework".to_string(),
                    serde_json::Value::String("dexcost-demo".to_string()),
                );
                m
            }),
            ..Default::default()
        },
    )
    .await?;
    let task_id = task.task().task_id.clone();
    println!("[dexcost] Task started: {}", task_id);

    // ── Step 1: Initial LLM call ─────────────────────────────────────────────
    let prompt_tokens: i64 = 150;
    let (output_tokens, latency_ms, should_retry) = simulate_llm_call(prompt_tokens);

    let llm_cost = Decimal::new(75, 5); // ~$0.00075 for local model
    let event = task
        .record_llm_call(
            "local",
            "local-llm",
            prompt_tokens,
            output_tokens,
            Some(llm_cost),
            None,
            Some(latency_ms),
        )
        .await?;
    println!(
        "[dexcost] LLM call recorded: {} input + {} output tokens, cost=${}, latency={}ms",
        prompt_tokens, output_tokens, event.cost_usd, latency_ms
    );

    // ── Step 2: Non-LLM tool calls ──────────────────────────────────────────
    let (service, cost, details) = simulate_tool_call("web_search");
    let tool_event = task
        .record_cost(
            service,
            cost,
            Some(details.clone()),
            Some(EventType::ExternalCost),
        )
        .await?;
    println!(
        "[dexcost] Tool cost recorded: {} cost=${}",
        service, tool_event.cost_usd
    );

    let (service2, cost2, details2) = simulate_tool_call("maps_api");
    let tool_event2 = task
        .record_cost(
            service2,
            cost2,
            Some(details2.clone()),
            Some(EventType::ExternalCost),
        )
        .await?;
    println!(
        "[dexcost] Tool cost recorded: {} cost=${}",
        service2, tool_event2.cost_usd
    );

    // ── Step 3: Retry waste tracking ────────────────────────────────────────
    if should_retry {
        println!("[dexcost] Simulated rate-limit — initiating retry...");
        // Record the retry event on the task (this is the waste).
        let retry_cost = llm_cost; // same cost for the retry call
        let retry_event = task.mark_retry("rate_limit_hit", retry_cost).await?;
        println!(
            "[dexcost] Retry waste recorded: reason={}, cost=${}",
            retry_event.retry_reason.as_ref().unwrap(),
            retry_event.cost_usd
        );
    }

    // ── End task and flush ──────────────────────────────────────────────────
    let status = if should_retry {
        TaskStatus::Failed
    } else {
        TaskStatus::Success
    };
    task.end(status).await?;
    flush().await?;

    // ── Print final summary ────────────────────────────────────────────────
    let t = task.task();
    println!();
    println!("=== Dexcost Agent Cost Capture Results ===");
    println!("Task ID:       {}", t.task_id);
    println!("Task Type:     {}", t.task_type);
    println!("Status:        {:?}", t.status);
    println!("LLM Cost:      ${}", t.llm_cost_usd);
    println!("Tool Costs:    ${}", t.external_cost_usd);
    println!("Total Cost:    ${}", t.total_cost_usd);
    println!("Input Tokens:  {}", t.total_input_tokens);
    println!("Output Tokens: {}", t.total_output_tokens);
    println!("Retry Count:   {}", t.retry_count);
    println!("Retry Waste:   ${}", t.retry_cost_usd);
    println!("Cached Tokens: {}", t.total_cached_tokens);
    println!("==========================================");

    // ── Verify event schema compliance ───────────────────────────────────────
    let buf = dexcost::buffer()?;
    let buf_guard = buf.lock().await;
    let events = buf_guard.query_events(&task_id);
    println!();
    println!("[dexcost] Events in buffer: {} events", events.len());
    for (i, ev) in events.iter().enumerate() {
        println!(
            "  Event {}: type={:?} cost=${} is_retry={} provider={:?} model={:?} service={:?}",
            i + 1,
            ev.event_type,
            ev.cost_usd,
            ev.is_retry,
            ev.provider.as_deref().unwrap_or("none"),
            ev.model.as_deref().unwrap_or("none"),
            ev.service_name.as_deref().unwrap_or("none"),
        );
        // Verify Standard Event Schema v1 required fields
        assert!(
            !ev.event_id.to_string().is_empty(),
            "event_id must be non-empty"
        );
        assert!(
            !ev.task_id.to_string().is_empty(),
            "task_id must be non-empty"
        );
        assert!(
            ev.cost_usd.scale() <= 10,
            "cost_usd must preserve precision"
        );
    }
    drop(buf_guard);

    println!();
    println!("[dexcost] All verifications passed.");
    close();
    Ok(())
}
