//! Fargate ECS task metadata reader.
//!
//! Hits `${ECS_CONTAINER_METADATA_URI_V4}/task` (or v3) once per process and
//! caches the parsed result. Exposes `vcpu_count` (f64) and
//! `memory_bytes_limit` (u64 — converted from MiB per Decision #7).
//!
//! Fail-silent contract: unreachable endpoint, malformed JSON, missing fields
//! all return `None`. Cached after first attempt (resolved=true).
//!
//! Mirrors `python/src/dexcost/fargate_metadata.py`.

use std::sync::{LazyLock, Mutex};
use std::time::Duration;

const PROBE_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, PartialEq)]
pub struct FargateTaskMetadata {
    pub vcpu_count: f64,
    pub memory_bytes_limit: u64,
}

struct CacheState {
    resolved: bool,
    cached: Option<FargateTaskMetadata>,
    warned: bool,
}

static CACHE: LazyLock<Mutex<CacheState>> = LazyLock::new(|| {
    Mutex::new(CacheState {
        resolved: false,
        cached: None,
        warned: false,
    })
});

fn endpoint() -> Option<String> {
    let base = std::env::var("ECS_CONTAINER_METADATA_URI_V4")
        .ok()
        .or_else(|| std::env::var("ECS_CONTAINER_METADATA_URI").ok())?;
    if base.is_empty() {
        return None;
    }
    Some(format!("{}/task", base.trim_end_matches('/')))
}

/// Read + cache the ECS task metadata. Idempotent.
///
/// Returns `None` when not on Fargate, when the endpoint is unreachable,
/// or when the `Limits` block is missing / malformed.
pub fn fetch_fargate_metadata() -> Option<FargateTaskMetadata> {
    {
        let guard = CACHE.lock().expect("fargate cache poisoned");
        if guard.resolved {
            return guard.cached.clone();
        }
    }

    let url = match endpoint() {
        Some(u) => u,
        None => {
            let mut guard = CACHE.lock().expect("fargate cache poisoned");
            guard.resolved = true;
            return None;
        }
    };

    let client = match reqwest::blocking::Client::builder()
        .timeout(PROBE_TIMEOUT)
        .build()
    {
        Ok(c) => c,
        Err(_) => {
            let mut guard = CACHE.lock().expect("fargate cache poisoned");
            guard.resolved = true;
            return None;
        }
    };

    let payload: serde_json::Value = match client.get(&url).send().and_then(|r| r.text()) {
        Ok(body) => match serde_json::from_str(&body) {
            Ok(v) => v,
            Err(_) => {
                let mut guard = CACHE.lock().expect("fargate cache poisoned");
                guard.resolved = true;
                if !guard.warned {
                    guard.warned = true;
                    eprintln!(
                        "[dexcost] WARNING: fargate metadata malformed; \
                         compute cost will fall through to default rates"
                    );
                }
                return None;
            }
        },
        Err(_) => {
            let mut guard = CACHE.lock().expect("fargate cache poisoned");
            guard.resolved = true;
            if !guard.warned {
                guard.warned = true;
                eprintln!(
                    "[dexcost] WARNING: fargate metadata unreachable; \
                     compute cost will fall through to default rates"
                );
            }
            return None;
        }
    };

    let limits = payload.get("Limits");
    let cpu = limits.and_then(|l| l.get("CPU")).and_then(|v| v.as_f64());
    let mem = limits
        .and_then(|l| l.get("Memory"))
        .and_then(|v| v.as_u64());

    let (vcpu, mem_mib) = match (cpu, mem) {
        (Some(c), Some(m)) => (c, m),
        _ => {
            let mut guard = CACHE.lock().expect("fargate cache poisoned");
            guard.resolved = true;
            return None;
        }
    };

    // Decision #7 — Fargate memory is in MiB (binary), NOT MB. Convert via
    // binary divisor (~4.86% silent over-attribution if decimal MB is used).
    let memory_bytes_limit = mem_mib.saturating_mul(1024 * 1024);

    let result = FargateTaskMetadata {
        vcpu_count: vcpu,
        memory_bytes_limit,
    };
    let mut guard = CACHE.lock().expect("fargate cache poisoned");
    guard.cached = Some(result.clone());
    guard.resolved = true;
    Some(result)
}

/// Test-only: clear cached state per convention §11.
#[doc(hidden)]
pub fn reset_for_tests() {
    let mut guard = CACHE.lock().expect("fargate cache poisoned");
    guard.resolved = false;
    guard.cached = None;
    guard.warned = false;
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{LazyLock, Mutex as StdMutex};
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    fn full_reset() {
        reset_for_tests();
        // SAFETY: tests serialize via TEST_LOCK.
        unsafe {
            std::env::remove_var("ECS_CONTAINER_METADATA_URI_V4");
            std::env::remove_var("ECS_CONTAINER_METADATA_URI");
        }
    }

    fn set_env(k: &str, v: &str) {
        unsafe { std::env::set_var(k, v) };
    }

    // Use a Tokio runtime for wiremock since blocking reqwest inside tokio
    // runtimes interacts oddly — we use a dedicated runtime in each test.
    fn start_mock_server(body: serde_json::Value, status: u16) -> (MockServer, String) {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let (server, uri) = rt.block_on(async {
            let server = MockServer::start().await;
            let uri = server.uri();
            Mock::given(method("GET"))
                .and(path("/task"))
                .respond_with(ResponseTemplate::new(status).set_body_json(body))
                .mount(&server)
                .await;
            (server, uri)
        });
        // Leak rt to keep mock alive for blocking req.
        std::mem::forget(rt);
        (server, uri)
    }

    #[test]
    fn returns_none_when_no_env_vars() {
        let _g = lock();
        full_reset();
        assert!(fetch_fargate_metadata().is_none());
    }

    #[test]
    fn caches_first_resolution_for_no_env() {
        let _g = lock();
        full_reset();
        let _ = fetch_fargate_metadata();
        set_env(
            "ECS_CONTAINER_METADATA_URI_V4",
            "http://invalid.local/v4/id",
        );
        // resolved=true already, so still None even though env now set.
        assert!(fetch_fargate_metadata().is_none());
    }

    #[test]
    fn parses_limits_to_metadata_with_binary_mib() {
        let _g = lock();
        full_reset();
        let body = serde_json::json!({
            "Limits": { "CPU": 2.0, "Memory": 4096 }
        });
        let (_server, uri) = start_mock_server(body, 200);
        set_env("ECS_CONTAINER_METADATA_URI_V4", &uri);
        let md = fetch_fargate_metadata().expect("metadata parses");
        assert!((md.vcpu_count - 2.0).abs() < 1e-9);
        // 4096 MiB * 1024 * 1024 = 4_294_967_296 bytes (Decision #7).
        assert_eq!(md.memory_bytes_limit, 4096u64 * 1024 * 1024);
    }

    #[test]
    fn caches_result_after_first_fetch() {
        let _g = lock();
        full_reset();
        let body = serde_json::json!({
            "Limits": { "CPU": 1.0, "Memory": 2048 }
        });
        let (_server, uri) = start_mock_server(body, 200);
        set_env("ECS_CONTAINER_METADATA_URI_V4", &uri);
        let first = fetch_fargate_metadata().expect("first fetch");
        let second = fetch_fargate_metadata().expect("cached fetch");
        assert_eq!(first, second);
    }

    #[test]
    fn malformed_limits_returns_none() {
        let _g = lock();
        full_reset();
        let body = serde_json::json!({
            "SomethingElse": {}
        });
        let (_server, uri) = start_mock_server(body, 200);
        set_env("ECS_CONTAINER_METADATA_URI_V4", &uri);
        assert!(fetch_fargate_metadata().is_none());
    }

    #[test]
    fn missing_cpu_field_returns_none() {
        let _g = lock();
        full_reset();
        let body = serde_json::json!({
            "Limits": { "Memory": 2048 }
        });
        let (_server, uri) = start_mock_server(body, 200);
        set_env("ECS_CONTAINER_METADATA_URI_V4", &uri);
        assert!(fetch_fargate_metadata().is_none());
    }
}
