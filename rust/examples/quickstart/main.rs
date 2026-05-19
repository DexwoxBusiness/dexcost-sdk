//! Dexcost Rust SDK — Quickstart Example
//!
//! Run with: cargo run --example quickstart
//!
//! This example demonstrates:
//!   1. Initializing the dexcost SDK
//!   2. Creating and tracking a business task
//!   3. Recording an LLM call and an external cost
//!   4. Ending the task and printing aggregated cost to stdout

use dexcost::core::models::TaskStatus;
use dexcost::core::tracker::TaskOptions;
use dexcost::{close, flush, init, start_task, Config};
use rust_decimal::Decimal;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize SDK with default configuration.
    // Uses in-memory buffer when no API key is set (offline/dev mode).
    init(Config::default())?;

    // Create a task with customer and project attribution.
    let mut task = start_task(
        "resolve_ticket",
        TaskOptions {
            customer_id: Some("acme-corp".into()),
            project_id: Some("support".into()),
            ..Default::default()
        },
    )
    .await?;

    // Record an LLM call with explicit cost ($0.05).
    // The SDK tracks tokens and cost per call for later analysis.
    let llm_cost = Decimal::new(5, 2); // 0.05 USD
    let llm_event = task
        .record_llm_call(
            "openai",
            "gpt-4o",
            1_000, // input tokens
            500,   // output tokens
            Some(llm_cost),
            None,      // cached tokens
            Some(250), // latency_ms
        )
        .await?;
    println!(
        "[dexcost] LLM call recorded: {} tokens, cost=${}",
        llm_event.input_tokens.unwrap_or(0) + llm_event.output_tokens.unwrap_or(0),
        llm_event.cost_usd
    );

    // Record an external service cost ($0.01 for a hypothetical Maps API call).
    let external_cost = Decimal::new(1, 2); // 0.01 USD
    let external_event = task
        .record_cost("google_maps", external_cost, None, None)
        .await?;
    println!(
        "[dexcost] External cost recorded: {} cost=${}",
        external_event.service_name.as_deref().unwrap_or("unknown"),
        external_event.cost_usd
    );

    // End the task successfully.
    task.end(TaskStatus::Success).await?;

    // Flush buffered events (no-op in offline mode, sends to API if configured).
    flush().await?;

    // Print aggregated task summary to stdout.
    let t = task.task();
    println!();
    println!("=== Dexcost Quickstart Results ===");
    println!("Task ID:    {}", t.task_id);
    println!("Task Type:  {}", t.task_type);
    println!("Status:     {:?}", t.status);
    println!("LLM Cost:   ${}", t.llm_cost_usd);
    println!("External:   ${}", t.external_cost_usd);
    println!("Total:      ${}", t.total_cost_usd);
    println!("Input:      {} tokens", t.total_input_tokens);
    println!("Output:     {} tokens", t.total_output_tokens);
    println!("Retries:    {} (cost ${})", t.retry_count, t.retry_cost_usd);
    println!("==================================");

    close();
    Ok(())
}
