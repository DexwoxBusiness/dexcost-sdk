//! # dexcost
//!
//! Rust SDK for [dexcost](https://github.com/DexwoxBusiness/dexcost) -- an Agent
//! Unit Economics platform. Track LLM costs, non-LLM service fees, and retry waste
//! attributed to customers, projects, and workflows.
//!
//! ## Quickstart
//!
//! ```no_run
//! use dexcost::{Config, TaskOptions, TaskStatus, init, start_task, flush, close};
//!
//! #[tokio::main]
//! async fn main() {
//!     init(Config::default()).unwrap();
//!
//!     let mut task = start_task("resolve_ticket", TaskOptions {
//!         customer_id: Some("acme-corp".into()),
//!         project_id: Some("support".into()),
//!         ..Default::default()
//!     }).await.unwrap();
//!
//!     task.record_llm_call("openai", "gpt-4o", 1000, 500, None, None, None)
//!         .await
//!         .unwrap();
//!
//!     task.end(TaskStatus::Success).await.unwrap();
//!     flush().await.unwrap();
//!     close();
//! }
//! ```
//!
//! See [`PARITY.md`](https://github.com/DexwoxBusiness/dexcost/blob/master/sdks/rust/PARITY.md)
//! for the Python ↔ Rust parity matrix and a list of idiomatic differences.

pub mod adapters;
pub mod clients;
pub mod config;
pub mod core;
pub mod dev_console;
pub mod error;
pub mod integrations;
pub mod middleware;
pub mod pricing;
pub mod scanner;
pub mod schema;
pub mod security;
pub mod transport;

use std::sync::{Arc, OnceLock};

use tokio::sync::Mutex;

// Re-exports — the public top-level surface of the SDK.
// Mirrors the Python SDK's `__all__` plus Rust-idiomatic additions.
// See `PARITY.md` for the full mapping.
pub use config::Config;
pub use core::context::{
    clear_dexcost_context, create_auto_task, get_current_task, get_dexcost_context, set_context,
    with_task, DexcostContext,
};
pub use core::heuristics::RetryHeuristicEngine;
pub use core::models::{CostConfidence, CostEvent, EventType, PricingSource, Task, TaskStatus};
pub use core::session::{get_session_manager, SessionManager};
pub use core::tracker::{
    HeuristicConfig, RecordCostOptions, RecordLlmCallOptions, TaskOptions, TrackedTask,
};
pub use error::DexcostError;
pub use pricing::engine::{CostResult, PricingEngine};
pub use pricing::rates::{RateEntry, RateRegistry};
pub use pricing::service_catalog::ServiceCatalog;
pub use schema::validate::validate;
pub use security::redaction::{enforce_metadata_limit, hash_value, redact_map};
pub use transport::buffer::EventBuffer;
pub use transport::pusher::EventPusher;

pub use clients::tracked_anthropic::TrackedAnthropic;
pub use clients::tracked_gemini::TrackedGemini;
pub use clients::tracked_openai::TrackedOpenAI;

/// SDK version, sourced from `Cargo.toml`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Identifiers for which dedicated tracked client wrappers are shipped.
/// These are wrapper/instrument names — note that `event.provider` for some
/// wrappers may differ (e.g. `TrackedGemini` emits `"google"`).
pub const ALL_SUPPORTED_INSTRUMENTS: &[&str] = &["openai", "anthropic", "gemini"];

/// Global SDK state.
struct SdkState {
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Arc<Mutex<PricingEngine>>,
    rate_registry: Arc<Mutex<RateRegistry>>,
    /// Service catalog for non-LLM HTTP cost extraction. `None` when
    /// `track_http` is disabled.
    service_catalog: Option<Arc<Mutex<ServiceCatalog>>>,
    #[allow(dead_code)]
    config: Config,
    pusher: Option<EventPusher>,
}

static GLOBAL_STATE: OnceLock<SdkState> = OnceLock::new();

/// Initializes the global dexcost SDK. Must be called before `start_task`.
/// Safe to call multiple times; only the first call takes effect.
pub fn init(mut config: Config) -> Result<(), DexcostError> {
    config.validate()?;

    // Use a file-backed SQLite buffer. Resolution order:
    //   1. `config.buffer_path` (explicit)
    //   2. `DEXCOST_BUFFER_PATH` env var
    //   3. `~/.dexcost/buffer.db`
    // Fall back to in-memory if the home directory cannot be determined.
    let buffer = {
        let db_path = config
            .buffer_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned())
            .or_else(|| std::env::var("DEXCOST_BUFFER_PATH").ok())
            .or_else(|| {
                dirs_next::home_dir().map(|h| {
                    h.join(".dexcost")
                        .join("buffer.db")
                        .to_string_lossy()
                        .into_owned()
                })
            });
        match db_path {
            Some(path) => match EventBuffer::open(&path) {
                Ok(buf) => Arc::new(Mutex::new(buf)),
                Err(e) => {
                    eprintln!("[dexcost] WARNING: failed to open buffer at {}: {}, falling back to in-memory", path, e);
                    Arc::new(Mutex::new(EventBuffer::new()?))
                }
            },
            None => Arc::new(Mutex::new(EventBuffer::new()?)),
        }
    };
    let pricing = Arc::new(Mutex::new(PricingEngine::new()));
    let rate_registry = Arc::new(Mutex::new(RateRegistry::new()));

    // HTTP tracking — when enabled, build a service catalog for non-LLM cost
    // extraction. Mirrors Python `__init__.py:108-185` (`track_http=True`).
    let service_catalog = if config.track_http {
        Some(Arc::new(Mutex::new(ServiceCatalog::new())))
    } else {
        None
    };

    // When a remote service catalog URL is configured, merge it in the
    // background (fail-silent — the bundled catalog remains usable).
    if let (Some(url), Some(catalog)) =
        (config.service_catalog_url.clone(), service_catalog.clone())
    {
        tokio::spawn(async move {
            let mut cat = catalog.lock().await;
            if let Err(e) = cat.refresh_from_url(&url).await {
                eprintln!(
                    "[dexcost] WARNING: service catalog refresh from {} failed: {}",
                    url, e
                );
            }
        });
    }

    let pusher = if config.api_key.is_some() {
        Some(EventPusher::new(buffer.clone(), config.clone()))
    } else {
        None
    };

    GLOBAL_STATE
        .set(SdkState {
            buffer,
            pricing,
            rate_registry,
            service_catalog,
            config,
            pusher,
        })
        .map_err(|_| DexcostError::AlreadyInitialized)?;

    // Start pusher background task if in cloud mode.
    // The JoinHandle is intentionally dropped to detach the background task.
    if let Some(state) = GLOBAL_STATE.get() {
        if let Some(ref p) = state.pusher {
            drop(p.start());
        }
    }

    Ok(())
}

/// Returns the global service catalog, or `None` when HTTP tracking is
/// disabled (`Config::track_http == false`).
pub fn service_catalog() -> Result<Option<Arc<Mutex<ServiceCatalog>>>, DexcostError> {
    let state = get_state()?;
    Ok(state.service_catalog.clone())
}

/// Returns a reference to the global state, or an error if not initialized.
fn get_state() -> Result<&'static SdkState, DexcostError> {
    GLOBAL_STATE.get().ok_or(DexcostError::NotInitialized)
}

/// Starts tracking a new task and returns a [`TrackedTask`].
///
/// If a parent task is present in the current task-local context, the new
/// task's `parent_task_id` is linked to it automatically. To make a task
/// discoverable as the parent of nested `start_task` calls, run the child
/// work inside [`TrackedTask::scope`] (or [`with_task`]):
///
/// ```no_run
/// # use dexcost::{start_task, TaskOptions, TaskStatus};
/// # async fn _example() -> Result<(), dexcost::DexcostError> {
/// let mut parent = start_task("parent", TaskOptions::default()).await?;
/// parent
///     .scope(async {
///         // auto-linked: child.parent_task_id == parent.task_id
///         let mut child = start_task("child", TaskOptions::default()).await?;
///         child.end(TaskStatus::Success).await
///     })
///     .await?;
/// parent.end(TaskStatus::Success).await?;
/// # Ok(())
/// # }
/// ```
pub async fn start_task(task_type: &str, opts: TaskOptions) -> Result<TrackedTask, DexcostError> {
    let state = get_state()?;

    let mut task = Task::new(task_type);
    task.status = TaskStatus::Running;
    task.customer_id = opts.customer_id;
    task.project_id = opts.project_id;
    task.experiment_id = opts.experiment_id;
    task.variant = opts.variant;

    if let Some(metadata) = opts.metadata {
        task.metadata = metadata;
    }

    // Link parent from task-local context if not explicitly set
    if opts.parent_task_id.is_some() {
        task.parent_task_id = opts.parent_task_id;
    } else if let Some(parent) = get_current_task() {
        task.parent_task_id = Some(parent.task_id);
    }

    // Insert into buffer
    {
        let mut buf = state.buffer.lock().await;
        buf.upsert_task(task.clone());
    }

    if let Some(hcfg) = opts.heuristics {
        let engine = Arc::new(std::sync::Mutex::new(RetryHeuristicEngine::new(
            hcfg.window_seconds,
            hcfg.threshold,
        )?));
        Ok(TrackedTask::with_heuristics(
            task,
            state.buffer.clone(),
            Some(state.pricing.clone()),
            Some(state.rate_registry.clone()),
            engine,
        ))
    } else {
        Ok(TrackedTask::with_rate_registry(
            task,
            state.buffer.clone(),
            Some(state.pricing.clone()),
            Some(state.rate_registry.clone()),
        ))
    }
}

/// Forces all buffered events to be pushed immediately.
pub async fn flush() -> Result<(), DexcostError> {
    let state = get_state()?;
    if let Some(ref pusher) = state.pusher {
        pusher.flush().await?;
    }
    Ok(())
}

/// Stops the background pusher and releases resources.
/// Note: Due to OnceLock, the SDK cannot be re-initialized after close().
pub fn close() {
    if let Some(state) = GLOBAL_STATE.get() {
        if let Some(ref pusher) = state.pusher {
            pusher.stop();
        }
    }
}

/// Returns the global event buffer (for advanced usage / testing).
pub fn buffer() -> Result<Arc<Mutex<EventBuffer>>, DexcostError> {
    let state = get_state()?;
    Ok(state.buffer.clone())
}

/// Returns the global pricing engine (for advanced usage / testing).
pub fn pricing_engine() -> Result<Arc<Mutex<PricingEngine>>, DexcostError> {
    let state = get_state()?;
    Ok(state.pricing.clone())
}

/// Returns the global rate registry (for advanced usage / testing).
pub fn rate_registry() -> Result<Arc<Mutex<RateRegistry>>, DexcostError> {
    let state = get_state()?;
    Ok(state.rate_registry.clone())
}
