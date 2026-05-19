//! Tower-based middleware for capturing per-request task latency and cost.
//!
//! Implements a [`tower::Layer`] / [`tower::Service`] pair that creates a
//! dexcost task for each HTTP request, records its latency, and ends the
//! task when the response is returned. The latency is attached as a trace
//! link so downstream sinks can correlate it with the underlying span.
//!
//! This is the [`tower`]-flavoured counterpart to [`crate::middleware::axum`]
//! and is intended for any framework that composes `tower::Service` (Hyper,
//! Tonic, Axum's lower layers, etc.).
//!
//! # Feature flag
//!
//! This module is gated behind the `tower-middleware` feature.
//!
//! # Example
//!
//! ```ignore
//! use std::sync::Arc;
//! use tokio::sync::Mutex;
//! use tower::ServiceBuilder;
//! use dexcost::middleware::tower::DexcostLayer;
//! use dexcost::transport::buffer::EventBuffer;
//!
//! let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
//! let svc = ServiceBuilder::new()
//!     .layer(DexcostLayer::new(buffer.clone(), None))
//!     .service(my_inner_service);
//! ```

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use tokio::sync::Mutex;
use tower::{Layer, Service};

use crate::core::models::{Task, TaskStatus};
use crate::core::tracker::TrackedTask;
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Tower [`Layer`] that wraps an inner service with [`DexcostService`].
///
/// All inputs are `Arc`-shared so the layer is cheap to clone across many
/// services.
#[derive(Clone)]
pub struct DexcostLayer {
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
}

impl DexcostLayer {
    pub fn new(
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Option<Arc<Mutex<PricingEngine>>>,
    ) -> Self {
        Self { buffer, pricing }
    }
}

impl<S> Layer<S> for DexcostLayer {
    type Service = DexcostService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        DexcostService {
            inner,
            buffer: self.buffer.clone(),
            pricing: self.pricing.clone(),
        }
    }
}

/// Tower [`Service`] that wraps another service. For every request that
/// implements `RequestInfo` (any `http::Request<B>` does, via the blanket impl
/// below), a dexcost task is created, the inner service is invoked, and the
/// task is finalised with success/failed status based on the response code.
#[derive(Clone)]
pub struct DexcostService<S> {
    inner: S,
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
}

/// Trait used to extract a task name + status from request/response pairs.
/// Implemented for `http::Request<B>` / `http::Response<B>` out of the box;
/// downstream users may implement it for their own request types.
pub trait RequestInfo {
    fn task_type(&self) -> String;
}

pub trait ResponseInfo {
    fn status_code(&self) -> u16;
}

impl<B> RequestInfo for ::http::Request<B> {
    fn task_type(&self) -> String {
        format!("{} {}", self.method(), self.uri().path())
    }
}

impl<B> ResponseInfo for ::http::Response<B> {
    fn status_code(&self) -> u16 {
        self.status().as_u16()
    }
}

impl<S, ReqBody, ResBody> Service<::http::Request<ReqBody>> for DexcostService<S>
where
    S: Service<::http::Request<ReqBody>, Response = ::http::Response<ResBody>> + Send + 'static,
    S::Future: Send + 'static,
    S::Error: Send + 'static,
    ReqBody: Send + 'static,
    ResBody: Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, req: ::http::Request<ReqBody>) -> Self::Future {
        let task_type = req.task_type();
        let buffer = self.buffer.clone();
        let pricing = self.pricing.clone();

        let fut = self.inner.call(req);

        Box::pin(async move {
            let task = Task::new(&task_type);
            let mut tracked = TrackedTask::new(task, buffer, pricing);

            let started = std::time::Instant::now();
            let result = fut.await;
            let latency_ms = started.elapsed().as_millis() as i64;

            tracked.link_trace("http", &format!("latency_ms={}", latency_ms));

            match &result {
                Ok(resp) => {
                    let status = if resp.status_code() >= 500 {
                        TaskStatus::Failed
                    } else {
                        TaskStatus::Success
                    };
                    let _ = tracked.end(status).await;
                }
                Err(_) => {
                    let _ = tracked.end(TaskStatus::Failed).await;
                }
            }
            result
        })
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::convert::Infallible;
    use tower::ServiceExt;

    /// A trivial inner service that echoes a status code.
    #[derive(Clone)]
    struct Echo {
        status: u16,
    }

    impl Service<::http::Request<()>> for Echo {
        type Response = ::http::Response<()>;
        type Error = Infallible;
        type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

        fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
            Poll::Ready(Ok(()))
        }

        fn call(&mut self, _req: ::http::Request<()>) -> Self::Future {
            let st = self.status;
            Box::pin(async move { Ok(::http::Response::builder().status(st).body(()).unwrap()) })
        }
    }

    fn fixture_buffer() -> Arc<Mutex<EventBuffer>> {
        Arc::new(Mutex::new(EventBuffer::new().expect("buffer")))
    }

    #[tokio::test]
    async fn layer_wraps_success_response() {
        let buffer = fixture_buffer();
        let layer = DexcostLayer::new(buffer.clone(), None);
        let svc = layer.layer(Echo { status: 200 });

        let req = ::http::Request::builder()
            .method("GET")
            .uri("/hello")
            .body(())
            .unwrap();
        let resp = svc.oneshot(req).await.expect("call");
        assert_eq!(resp.status(), 200);

        // Task must have been recorded and ended with success.
        let buf = buffer.lock().await;
        let tasks = buf.all_tasks();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].task_type, "GET /hello");
        assert_eq!(tasks[0].status, TaskStatus::Success);
    }

    #[tokio::test]
    async fn layer_marks_5xx_responses_as_failed() {
        let buffer = fixture_buffer();
        let layer = DexcostLayer::new(buffer.clone(), None);
        let svc = layer.layer(Echo { status: 503 });

        let req = ::http::Request::builder()
            .method("POST")
            .uri("/broken")
            .body(())
            .unwrap();
        let resp = svc.oneshot(req).await.expect("call");
        assert_eq!(resp.status(), 503);

        let buf = buffer.lock().await;
        let tasks = buf.all_tasks();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].status, TaskStatus::Failed);
        assert_eq!(tasks[0].task_type, "POST /broken");
    }

    #[tokio::test]
    async fn layer_records_latency_trace_link() {
        let buffer = fixture_buffer();
        let layer = DexcostLayer::new(buffer.clone(), None);
        let svc = layer.layer(Echo { status: 200 });

        let req = ::http::Request::builder().uri("/x").body(()).unwrap();
        let _ = svc.oneshot(req).await.expect("call");

        // `link_trace` records into the persisted task's metadata under
        // `_trace_links` so downstream sinks can correlate to the underlying
        // span. Verify the http-latency entry is present.
        let buf = buffer.lock().await;
        let tasks = buf.all_tasks();
        let task = &tasks[0];
        let links = task
            .metadata
            .get("_trace_links")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        assert!(
            links.iter().any(|l| {
                l.get("provider").and_then(|v| v.as_str()) == Some("http")
                    && l.get("trace_id")
                        .and_then(|v| v.as_str())
                        .map(|s| s.starts_with("latency_ms="))
                        .unwrap_or(false)
            }),
            "expected http latency trace link, got {:?}",
            links,
        );
    }
}
