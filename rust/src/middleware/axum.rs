#[cfg(feature = "axum-middleware")]
use std::sync::Arc;

#[cfg(feature = "axum-middleware")]
use axum::{extract::Request, middleware::Next, response::Response};

#[cfg(feature = "axum-middleware")]
use tokio::sync::Mutex;

#[cfg(feature = "axum-middleware")]
use crate::core::models::TaskStatus;
#[cfg(feature = "axum-middleware")]
use crate::core::tracker::TrackedTask;
#[cfg(feature = "axum-middleware")]
use crate::pricing::engine::PricingEngine;
#[cfg(feature = "axum-middleware")]
use crate::transport::buffer::EventBuffer;

/// Axum middleware that automatically creates a dexcost task for each HTTP request.
/// Records the request latency and ends the task with appropriate status.
///
/// # Example
///
/// ```ignore
/// use axum::{Router, middleware};
/// use dexcost::middleware::axum::dexcost_layer;
///
/// let buffer = Arc::new(Mutex::new(EventBuffer::new()));
/// let app = Router::new()
///     .layer(middleware::from_fn(move |req, next| {
///         dexcost_middleware(req, next, buffer.clone(), None)
///     }));
/// ```
#[cfg(feature = "axum-middleware")]
pub async fn dexcost_middleware(
    request: Request,
    next: Next,
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
) -> Response {
    let task_type = format!("{} {}", request.method(), request.uri().path());

    let task = crate::core::models::Task::new(&task_type);
    let mut tracked = TrackedTask::new(task, buffer, pricing);

    let start = std::time::Instant::now();
    let response = next.run(request).await;
    let latency_ms = start.elapsed().as_millis() as i64;

    tracked.link_trace("http", &format!("latency_ms={}", latency_ms));

    let status_code = response.status().as_u16();
    let task_status = if status_code >= 500 {
        TaskStatus::Failed
    } else {
        TaskStatus::Success
    };

    let _ = tracked.end(task_status).await;

    response
}
