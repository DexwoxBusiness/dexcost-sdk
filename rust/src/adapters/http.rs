//! HTTP adapter for dexcost.
//!
//! Rust cannot patch global HTTP libraries. Instead this module provides:
//!
//! - A **domain rate registry** — maps hostnames to per-call costs.
//! - A `record_http_cost` function that looks up the domain from a URL and
//!   appends a `CostEvent` to an in-process event log.
//!
//! Both the rate registry and the event log are guarded by `std::sync::Mutex`
//! and stored in `LazyLock` statics (stable since Rust 1.80).

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};

use rust_decimal::Decimal;
use uuid::Uuid;

use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource};

/// A cost rate associated with a specific HTTP domain.
#[derive(Debug, Clone)]
pub struct DomainRate {
    /// Cost in USD for a single call to this domain.
    pub cost_usd: Decimal,
    /// Unit description, e.g. `"call"`, `"request"`, `"1k tokens"`.
    pub per: String,
}

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

static DOMAIN_RATES: LazyLock<Mutex<HashMap<String, DomainRate>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

static RECORDED_EVENTS: LazyLock<Mutex<Vec<CostEvent>>> = LazyLock::new(|| Mutex::new(Vec::new()));

/// Process-wide lock serialising every test that touches the global
/// `DOMAIN_RATES` / `RECORDED_EVENTS` statics. Shared by the `http` and
/// `reqwest_middleware` test modules so they cannot race on this state.
#[cfg(test)]
pub(crate) static GLOBAL_HTTP_TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

// ---------------------------------------------------------------------------
// Domain rate registry
// ---------------------------------------------------------------------------

/// Registers a per-call cost for the given domain.
///
/// Overwrites any previously registered rate for the same domain.
///
/// ```rust
/// use dexcost::adapters::http::register_domain_rate;
/// use rust_decimal_macros::dec;
///
/// register_domain_rate("api.openai.com", dec!(0.002), "call");
/// ```
pub fn register_domain_rate(domain: &str, cost_usd: Decimal, per: &str) {
    let mut rates = DOMAIN_RATES.lock().unwrap_or_else(|e| {
        eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
        e.into_inner()
    });
    rates.insert(
        domain.to_string(),
        DomainRate {
            cost_usd,
            per: per.to_string(),
        },
    );
}

/// Returns a snapshot of all currently registered domain rates.
pub fn get_domain_rates() -> HashMap<String, DomainRate> {
    DOMAIN_RATES
        .lock()
        .unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        })
        .clone()
}

/// Removes all registered domain rates.
pub fn clear_domain_rates() {
    DOMAIN_RATES
        .lock()
        .unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        })
        .clear();
}

// ---------------------------------------------------------------------------
// HTTP cost recording
// ---------------------------------------------------------------------------

/// Extracts the hostname from a URL string.
///
/// Handles both `scheme://host/path` and bare `host/path` forms.  Returns
/// `None` if the hostname cannot be determined.
fn extract_hostname(url: &str) -> Option<String> {
    // Strip scheme if present.
    let without_scheme = if let Some(pos) = url.find("://") {
        &url[pos + 3..]
    } else {
        url
    };

    // Take the part before the first `/`, `?`, or `#`.
    let host_with_port = without_scheme.split(['/', '?', '#']).next()?;

    // Strip port number if present.
    let host = if let Some(bracket_end) = host_with_port.find(']') {
        // IPv6 literal: "[::1]:port" -> "[::1]"
        &host_with_port[..=bracket_end]
    } else if let Some(colon_pos) = host_with_port.rfind(':') {
        &host_with_port[..colon_pos]
    } else {
        host_with_port
    };

    if host.is_empty() {
        None
    } else {
        Some(host.to_lowercase())
    }
}

/// Resolves the cost event for an HTTP call **without recording it anywhere**.
///
/// Resolution order:
/// 1. The domain rate registry ([`register_domain_rate`]).
/// 2. The supplied `ServiceCatalog` — a fixed/endpoint-match cost is extracted
///    when the URL matches a catalog entry. `catalog` is `None` when no catalog
///    is available, in which case only the domain rate registry is consulted.
///
/// Returns `None` when neither source matches. This is the shared building
/// block behind both [`record_http_cost_with_catalog`] (in-memory log, for
/// standalone/manual use) and the `reqwest-middleware` adapter (durable
/// `EventBuffer`, so the sync pusher ships the event) — both routes therefore
/// emit identical events.
pub fn resolve_http_cost_event(
    url: &str,
    task_id: &str,
    catalog: Option<&crate::pricing::service_catalog::ServiceCatalog>,
) -> Option<CostEvent> {
    let hostname = extract_hostname(url)?;

    let rate = {
        let rates = DOMAIN_RATES.lock().unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        });
        rates.get(&hostname).cloned()
    };

    // 1. Domain rate registry match.
    if let Some(rate) = rate {
        let mut event = CostEvent::new(task_id, EventType::ExternalCost);
        event.event_id = Uuid::new_v4().to_string();
        event.cost_usd = rate.cost_usd;
        event.cost_confidence = CostConfidence::Computed;
        event.pricing_source = Some(PricingSource::RateRegistry);
        event.service_name = Some(hostname);
        return Some(event);
    }

    // 2. Service catalog fallback — no body/headers available here, so only
    //    fixed / endpoint-match / fallback extractions can resolve.
    let catalog = catalog?;
    let entry = catalog.lookup(url)?;
    catalog
        .extract_cost(entry, &HashMap::new(), None)
        .map(|extraction| {
            let mut event = CostEvent::new(task_id, EventType::ExternalCost);
            event.event_id = Uuid::new_v4().to_string();
            event.cost_usd = extraction.amount;
            event.cost_confidence = match extraction.confidence.as_str() {
                "exact" => CostConfidence::Exact,
                "computed" => CostConfidence::Computed,
                "estimated" => CostConfidence::Estimated,
                _ => CostConfidence::Unknown,
            };
            event.pricing_source = Some(match extraction.pricing_source.as_str() {
                "user_override" => PricingSource::UserOverride,
                _ => PricingSource::ServiceCatalog,
            });
            event.service_name = Some(extraction.service_name.clone());
            event
        })
}

/// Checks whether `url` matches a registered domain. If it does, a
/// `CostEvent` with `EventType::ExternalCost` is appended to the in-process
/// event log.
///
/// The function is synchronous and lock-based; it is safe to call from any
/// thread.
pub fn record_http_cost(url: &str, task_id: &str) {
    record_http_cost_with_catalog(url, task_id, None);
}

/// Like [`record_http_cost`], but consults a [`ServiceCatalog`] when the URL's
/// domain has no registered domain rate. Resolution is delegated to
/// [`resolve_http_cost_event`]; the event (if any) is appended to the in-memory
/// recording log. The `reqwest-middleware` adapter uses `resolve_http_cost_event`
/// directly so its events reach durable storage instead.
pub fn record_http_cost_with_catalog(
    url: &str,
    task_id: &str,
    catalog: Option<&crate::pricing::service_catalog::ServiceCatalog>,
) {
    if let Some(event) = resolve_http_cost_event(url, task_id, catalog) {
        RECORDED_EVENTS
            .lock()
            .unwrap_or_else(|e| {
                eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
                e.into_inner()
            })
            .push(event);
    }
}

// ---------------------------------------------------------------------------
// Recorded events log
// ---------------------------------------------------------------------------

/// Returns a snapshot of all cost events that have been recorded so far.
pub fn get_recorded_events() -> Vec<CostEvent> {
    RECORDED_EVENTS
        .lock()
        .unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        })
        .clone()
}

/// Clears the recorded events log.
pub fn clear_recorded_events() {
    RECORDED_EVENTS
        .lock()
        .unwrap_or_else(|e| {
            eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
            e.into_inner()
        })
        .clear();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Decimal {
        s.parse().expect("invalid decimal literal in test")
    }

    /// Guard that (a) serialises tests via the shared `GLOBAL_HTTP_TEST_LOCK`
    /// (the domain-rate and recorded-event stores are process-global statics)
    /// and (b) clears global state on creation and on drop.
    struct StateGuard<'a>(#[allow(dead_code)] tokio::sync::MutexGuard<'a, ()>);

    impl<'a> StateGuard<'a> {
        fn new() -> Self {
            let guard = GLOBAL_HTTP_TEST_LOCK.blocking_lock();
            clear_domain_rates();
            clear_recorded_events();
            StateGuard(guard)
        }
    }

    impl Drop for StateGuard<'_> {
        fn drop(&mut self) {
            clear_domain_rates();
            clear_recorded_events();
        }
    }

    #[test]
    fn test_register_and_get_rates() {
        let _g = StateGuard::new();

        register_domain_rate("api.openai.com", d("0.002"), "call");
        register_domain_rate("api.anthropic.com", d("0.005"), "request");

        let rates = get_domain_rates();
        assert_eq!(rates.len(), 2);

        let openai = rates.get("api.openai.com").expect("openai missing");
        assert_eq!(openai.cost_usd, d("0.002"));
        assert_eq!(openai.per, "call");

        let anthropic = rates.get("api.anthropic.com").expect("anthropic missing");
        assert_eq!(anthropic.cost_usd, d("0.005"));
        assert_eq!(anthropic.per, "request");
    }

    #[test]
    fn test_clear_domain_rates() {
        let _g = StateGuard::new();

        register_domain_rate("example.com", d("0.001"), "call");
        assert_eq!(get_domain_rates().len(), 1);

        clear_domain_rates();
        assert!(get_domain_rates().is_empty());
    }

    #[test]
    fn test_record_http_cost_when_domain_matches() {
        let _g = StateGuard::new();

        register_domain_rate("api.openai.com", d("0.003"), "call");
        record_http_cost("https://api.openai.com/v1/chat/completions", "task-abc");

        let events = get_recorded_events();
        assert_eq!(events.len(), 1);

        let ev = &events[0];
        assert_eq!(ev.task_id, "task-abc");
        assert_eq!(ev.cost_usd, d("0.003"));
        assert_eq!(ev.event_type, EventType::ExternalCost);
        assert_eq!(ev.cost_confidence, CostConfidence::Computed);
        assert_eq!(ev.pricing_source, Some(PricingSource::RateRegistry));
        assert_eq!(ev.service_name.as_deref(), Some("api.openai.com"));
    }

    #[test]
    fn test_no_record_when_domain_unmatched() {
        let _g = StateGuard::new();

        register_domain_rate("api.openai.com", d("0.003"), "call");
        record_http_cost("https://unknown.example.com/v1/endpoint", "task-xyz");

        assert!(get_recorded_events().is_empty());
    }

    // Gap 11: when no domain rate is registered, fall back to the catalog.
    #[test]
    fn test_record_http_cost_falls_back_to_catalog() {
        let _g = StateGuard::new();

        let catalog = crate::pricing::service_catalog::ServiceCatalog::new();
        // api.exa.ai is a known fixed-cost catalog entry; no domain rate is registered.
        record_http_cost_with_catalog("https://api.exa.ai/search", "task-cat", Some(&catalog));

        let events = get_recorded_events();
        assert_eq!(events.len(), 1, "catalog fallback should record one event");
        let ev = &events[0];
        assert_eq!(ev.task_id, "task-cat");
        assert!(ev.cost_usd >= Decimal::ZERO);
        assert_eq!(
            ev.pricing_source,
            Some(PricingSource::ServiceCatalog),
            "catalog-derived events use the ServiceCatalog pricing source"
        );
    }

    // Domain rate still takes precedence over the catalog.
    #[test]
    fn test_record_http_cost_domain_rate_wins_over_catalog() {
        let _g = StateGuard::new();

        register_domain_rate("api.exa.ai", d("0.123"), "call");
        let catalog = crate::pricing::service_catalog::ServiceCatalog::new();
        record_http_cost_with_catalog("https://api.exa.ai/search", "task-pref", Some(&catalog));

        let events = get_recorded_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].cost_usd, d("0.123"));
        assert_eq!(events[0].pricing_source, Some(PricingSource::RateRegistry));
    }

    #[test]
    fn test_get_and_clear_recorded_events() {
        let _g = StateGuard::new();

        register_domain_rate("api.example.com", d("0.001"), "call");
        record_http_cost("https://api.example.com/endpoint", "task-1");
        record_http_cost("https://api.example.com/endpoint", "task-2");

        assert_eq!(get_recorded_events().len(), 2);

        clear_recorded_events();
        assert!(get_recorded_events().is_empty());

        // Rates should still be registered after clearing events.
        assert!(!get_domain_rates().is_empty());
    }
}
