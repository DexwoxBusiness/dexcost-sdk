# dexcost

Rust SDK for [dexcost](https://github.com/DexwoxBusiness/dexcost-sdk) -- Agent Unit Economics platform. Track LLM costs, non-LLM service fees, and retry waste attributed to customers, projects, and workflows.

## Installation

```bash
cargo add dexcost
```

Or add to your `Cargo.toml`:

```toml
[dependencies]
dexcost = "0.1"
```

## Quickstart

```rust
use dexcost::{Config, TaskOptions, TaskStatus, init, start_task, flush, close};
use rust_decimal_macros::dec;

#[tokio::main]
async fn main() {
    // Initialize (local-only mode, no API key needed).
    init(Config::default()).unwrap();

    // Start a task.
    let mut task = start_task("resolve_ticket", TaskOptions {
        customer_id: Some("acme-corp".into()),
        project_id: Some("support".into()),
        ..Default::default()
    }).await.unwrap();

    // Record an LLM call (auto-priced from bundled model data).
    task.record_llm_call("openai", "gpt-4o", 1000, 500, None, None, None)
        .await
        .unwrap();

    // Record a non-LLM cost.
    task.record_cost("google_maps", dec!(0.005), None, None).await.unwrap();

    // Mark a retry.
    task.mark_retry("rate_limit", dec!(0.0)).await.unwrap();

    // End the task.
    task.end(TaskStatus::Success).await.unwrap();

    println!("Total cost: {} USD", task.task().total_cost_usd);

    flush().await.unwrap();
    close();
}
```

## Cloud Mode

To push events to the dexcost Control Layer:

```rust
use dexcost::{Config, init};

init(Config {
    api_key: Some("dx_live_your_key_here".into()), // or set DEXCOST_API_KEY env var
    ..Config::default()
}).unwrap();
```

Events are buffered in memory and pushed in batches every 5 seconds.

The Control Layer endpoint defaults to `https://api.dexcost.io`. To target a
different endpoint (e.g. a local server for testing), set `Config::endpoint`
explicitly in code:

```rust
init(Config {
    api_key: Some("dx_test_local".into()),
    endpoint: Some("http://localhost:8080".into()),
    ..Config::default()
}).unwrap();
```

The endpoint is read **only** from this in-code field — the SDK no longer reads
a `DEXCOST_ENDPOINT` environment variable, so a hostile process environment
cannot redirect telemetry or the API key.

## Features

- **LLM Cost Tracking** -- Record costs for any LLM provider with auto-pricing from bundled model data
- **Non-LLM Cost Tracking** -- Track external service costs (APIs, compute, storage)
- **Retry Waste Detection** -- First-class retry tracking with `is_retry`, `retry_reason`, `retry_of`
- **Customer Attribution** -- Attribute costs to customers, projects, and workflows
- **A/B Testing** -- Tag tasks with `experiment_id` and `variant` for cost comparison
- **Nested Tasks** -- Link parent/child tasks via task-local context
- **Auto-Pricing** -- Bundled pricing data for 1000+ LLM models (LiteLLM cost map)
- **Custom Pricing** -- Override bundled rates or register custom per-1k-token pricing
- **Background Sync** -- Events buffered in memory and pushed to control layer in batches
- **Axum Middleware** -- Optional middleware for automatic HTTP request tracking (enable `axum-middleware` feature)

## Custom Pricing

Override bundled LLM pricing:

```rust
use dexcost::pricing_engine;
use rust_decimal_macros::dec;

let engine = pricing_engine().unwrap();
let mut engine = engine.lock().await;
engine.set_custom_pricing("my-model", dec!(0.001), dec!(0.002));
```

## Axum Middleware

Enable the `axum-middleware` feature in `Cargo.toml`:

```toml
[dependencies]
dexcost = { version = "0.1", features = ["axum-middleware"] }
```

Then add the middleware to your Axum router:

```rust,ignore
use axum::{Router, middleware};
use dexcost::middleware::axum::dexcost_middleware;

let app = Router::new()
    .layer(middleware::from_fn(move |req, next| {
        dexcost_middleware(req, next, buffer.clone(), None)
    }));
```

## Key Design Decisions

- All costs use `rust_decimal::Decimal` -- never f64 for money
- Costs serialized as strings in JSON output for precision
- UUIDs generated via `uuid::Uuid::v4()`
- Retry waste is a first-class metric (`is_retry`, `retry_reason`, `retry_of`)
- Schema v1 compatible with the Python, TypeScript, and Go SDKs
- Thread-safe: `Arc<Mutex<>>` for shared state

## Testing

```bash
cargo test
```

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
