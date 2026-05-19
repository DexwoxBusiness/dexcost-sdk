//! Actix-web middleware for capturing per-request task latency and cost.
//!
//! Implements the [`actix_service::Transform`] / [`actix_service::Service`]
//! pair that creates a dexcost task for each HTTP request, records its
//! latency, and ends the task when the response is returned.
//!
//! This is the [`actix_web`]-flavoured counterpart to
//! [`crate::middleware::axum`] and [`crate::middleware::tower`].
//!
//! # Feature flag
//!
//! This module is gated behind the `actix-middleware` feature.
//!
//! # Example
//!
//! ```ignore
//! use std::sync::Arc;
//! use tokio::sync::Mutex;
//! use actix_web::{App, HttpServer};
//! use dexcost::middleware::actix::DexcostMiddleware;
//! use dexcost::transport::buffer::EventBuffer;
//!
//! #[actix_web::main]
//! async fn main() -> std::io::Result<()> {
//!     let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
//!     HttpServer::new(move || {
//!         App::new().wrap(DexcostMiddleware::new(buffer.clone(), None))
//!     })
//!     .bind("127.0.0.1:8080")?
//!     .run()
//!     .await
//! }
//! ```

use std::future::{ready, Future, Ready};
use std::pin::Pin;
use std::rc::Rc;
use std::sync::Arc;
use std::task::{Context, Poll};

use actix_service::{Service, Transform};
use actix_web::body::MessageBody;
use actix_web::dev::{ServiceRequest, ServiceResponse};
use actix_web::Error;
use tokio::sync::Mutex;

use crate::core::models::{Task, TaskStatus};
use crate::core::tracker::TrackedTask;
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Actix-web middleware factory. Construct via [`DexcostMiddleware::new`] and
/// register with `actix_web::App::wrap`.
#[derive(Clone)]
pub struct DexcostMiddleware {
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
}

impl DexcostMiddleware {
    pub fn new(
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Option<Arc<Mutex<PricingEngine>>>,
    ) -> Self {
        Self { buffer, pricing }
    }
}

impl<S, B> Transform<S, ServiceRequest> for DexcostMiddleware
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type Transform = DexcostMiddlewareService<S>;
    type InitError = ();
    type Future = Ready<Result<Self::Transform, Self::InitError>>;

    fn new_transform(&self, service: S) -> Self::Future {
        ready(Ok(DexcostMiddlewareService {
            service: Rc::new(service),
            buffer: self.buffer.clone(),
            pricing: self.pricing.clone(),
        }))
    }
}

/// The wrapped service produced by [`DexcostMiddleware::new_transform`].
pub struct DexcostMiddlewareService<S> {
    service: Rc<S>,
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
}

impl<S, B> Service<ServiceRequest> for DexcostMiddlewareService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>>>>;

    fn poll_ready(&self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.service.poll_ready(cx)
    }

    fn call(&self, req: ServiceRequest) -> Self::Future {
        let task_type = format!("{} {}", req.method(), req.path());
        let buffer = self.buffer.clone();
        let pricing = self.pricing.clone();
        let svc = self.service.clone();

        Box::pin(async move {
            let task = Task::new(&task_type);
            let mut tracked = TrackedTask::new(task, buffer, pricing);

            let started = std::time::Instant::now();
            let result = svc.call(req).await;
            let latency_ms = started.elapsed().as_millis() as i64;

            tracked.link_trace("http", &format!("latency_ms={}", latency_ms));

            match &result {
                Ok(resp) => {
                    let status = if resp.status().as_u16() >= 500 {
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
    use actix_web::{test, web, App, HttpResponse};

    fn fixture_buffer() -> Arc<Mutex<EventBuffer>> {
        Arc::new(Mutex::new(EventBuffer::new().expect("buffer")))
    }

    #[actix_web::test]
    async fn middleware_creates_task_for_success_request() {
        let buffer = fixture_buffer();
        let mw = DexcostMiddleware::new(buffer.clone(), None);

        let app = test::init_service(App::new().wrap(mw).route(
            "/hello",
            web::get().to(|| async { HttpResponse::Ok().body("hi") }),
        ))
        .await;

        let req = test::TestRequest::get().uri("/hello").to_request();
        let resp = test::call_service(&app, req).await;
        assert_eq!(resp.status().as_u16(), 200);

        let buf = buffer.lock().await;
        let tasks = buf.all_tasks();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].task_type, "GET /hello");
        assert_eq!(tasks[0].status, TaskStatus::Success);
    }

    #[actix_web::test]
    async fn middleware_marks_5xx_responses_as_failed() {
        let buffer = fixture_buffer();
        let mw = DexcostMiddleware::new(buffer.clone(), None);

        let app = test::init_service(App::new().wrap(mw).route(
            "/bad",
            web::post().to(|| async { HttpResponse::InternalServerError().body("oops") }),
        ))
        .await;

        let req = test::TestRequest::post().uri("/bad").to_request();
        let resp = test::call_service(&app, req).await;
        assert_eq!(resp.status().as_u16(), 500);

        let buf = buffer.lock().await;
        let tasks = buf.all_tasks();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].task_type, "POST /bad");
        assert_eq!(tasks[0].status, TaskStatus::Failed);
    }

    #[actix_web::test]
    async fn middleware_records_latency_link() {
        let buffer = fixture_buffer();
        let mw = DexcostMiddleware::new(buffer.clone(), None);

        let app = test::init_service(App::new().wrap(mw).route(
            "/x",
            web::get().to(|| async { HttpResponse::Ok().finish() }),
        ))
        .await;

        let req = test::TestRequest::get().uri("/x").to_request();
        let _ = test::call_service(&app, req).await;

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
