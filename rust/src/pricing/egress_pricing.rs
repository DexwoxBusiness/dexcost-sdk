//! Egress pricing engine — resolves a per-GB egress rate from
//! `(provider, region)` using the bundled `data/egress_prices.json` catalog.
//!
//! Mirrors the Python [`dexcost.egress_pricing`] module shape: bundled JSON +
//! a resolver returning `(rate, pricing_source, cost_confidence)`.
//!
//! Fail-silent contract: every failure mode degrades through the spec §7.1
//! ladder; the engine always returns a usable [`EgressRate`].

use std::collections::HashSet;
use std::str::FromStr;
use std::sync::{LazyLock, Mutex};

use rust_decimal::Decimal;
use serde_json::Value;

/// The bundled catalog, embedded at compile time. Mirrors Python's
/// `importlib.resources` lookup.
const BUNDLED_CATALOG: &str = include_str!("../data/egress_prices.json");

/// Tier-4 ultimate fallback — used only when the catalog cannot be read at
/// all AND `_meta.default_rate_usd_per_gb` cannot be resolved. Matches the
/// spec §7.1 hardcoded constant.
fn hardcoded_default() -> Decimal {
    Decimal::from_str_exact("0.09").expect("0.09 is a valid Decimal")
}

/// Module-level warn-once tracking, keyed by failure-mode token.
static WARNED_MODES: LazyLock<Mutex<HashSet<String>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

fn warn_once(mode: &str, message: &str) {
    let mut guard = WARNED_MODES.lock().expect("warn-once mutex poisoned");
    if guard.contains(mode) {
        return;
    }
    guard.insert(mode.to_string());
    drop(guard);
    eprintln!("[dexcost] WARNING: {}", message);
}

/// Test-only: clear the warn-once tracking set.
#[doc(hidden)]
pub fn reset_warning_state_for_tests() {
    let mut guard = WARNED_MODES.lock().expect("warn-once mutex poisoned");
    guard.clear();
}

/// The result of an egress-rate lookup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EgressRate {
    pub rate_per_gb: Decimal,
    pub pricing_source: String,
    /// One of `"exact"`, `"computed"`, `"estimated"`.
    pub cost_confidence: String,
}

/// Resolve egress rates from the bundled catalog.
#[derive(Debug, Clone)]
pub struct EgressPricingEngine {
    catalog: Value,
    catalog_version: String,
}

impl Default for EgressPricingEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl EgressPricingEngine {
    /// Loads from the bundled `egress_prices.json`.
    pub fn new() -> Self {
        Self::from_str(BUNDLED_CATALOG)
    }

    /// Loads from a caller-supplied JSON string (used for test overrides and
    /// the "catalog unreadable" tiers).
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(catalog_json: &str) -> Self {
        let catalog: Value = match serde_json::from_str(catalog_json) {
            Ok(v) => v,
            Err(e) => {
                warn_once(
                    "catalog_malformed",
                    &format!(
                        "egress catalog malformed JSON ({}); falling back to hardcoded default",
                        e
                    ),
                );
                return Self {
                    catalog: Value::Null,
                    catalog_version: "unknown".to_string(),
                };
            }
        };

        let version = catalog
            .get("_meta")
            .and_then(|m| m.get("version"))
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();

        Self {
            catalog,
            catalog_version: version,
        }
    }

    /// Loads a catalog from a filesystem path. If the file is missing or
    /// unreadable, the engine falls through to the hardcoded default at lookup
    /// time. Mirrors the `catalog_path` override in the Python engine.
    pub fn from_path(path: &std::path::Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(raw) => Self::from_str(&raw),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                warn_once(
                    "catalog_missing",
                    "egress catalog file not found; falling back to hardcoded default",
                );
                Self {
                    catalog: Value::Null,
                    catalog_version: "unknown".to_string(),
                }
            }
            Err(e) => {
                warn_once(
                    "catalog_unreadable",
                    &format!(
                        "egress catalog unreadable ({}); falling back to hardcoded default",
                        e
                    ),
                );
                Self {
                    catalog: Value::Null,
                    catalog_version: "unknown".to_string(),
                }
            }
        }
    }

    pub fn catalog_version(&self) -> &str {
        &self.catalog_version
    }

    /// Rate for a call classified as internal traffic — always free.
    pub fn rate_for_internal(&self) -> EgressRate {
        EgressRate {
            rate_per_gb: Decimal::ZERO,
            pricing_source: "egress_catalog:internal".to_string(),
            cost_confidence: "exact".to_string(),
        }
    }

    /// Resolve an egress rate via the §7.1 degradation ladder.
    ///
    /// - Tier 1: `(provider, region)` exact catalog match → region rate, `computed`
    /// - Tier 2: provider known, region missing → provider default, `estimated`
    /// - Tier 3: provider unknown / not in catalog → `_meta` default, `estimated`
    /// - Tier 4: catalog unreadable / meta default missing → hardcoded `0.09`, `estimated`
    pub fn resolve_rate(&self, provider: Option<&str>, region: Option<&str>) -> EgressRate {
        if let Some(prov) = provider {
            if let Some(block) = self.catalog.get(prov) {
                if block.is_object() {
                    // Tier 1: exact (provider, region) match.
                    if let Some(reg) = region {
                        if let Some(rate_val) =
                            block.get("regions").and_then(|r| r.get(reg))
                        {
                            let s = match rate_val {
                                Value::String(s) => Some(s.as_str().to_string()),
                                Value::Number(n) => Some(n.to_string()),
                                _ => None,
                            };
                            if let Some(s) = s {
                                match Decimal::from_str(&s) {
                                    Ok(rate) => {
                                        return EgressRate {
                                            rate_per_gb: rate,
                                            pricing_source: format!(
                                                "egress_catalog:{}:{}",
                                                prov, reg
                                            ),
                                            cost_confidence: "computed".to_string(),
                                        };
                                    }
                                    Err(_) => {
                                        warn_once(
                                            &format!(
                                                "region_rate_malformed:{}:{}",
                                                prov, reg
                                            ),
                                            &format!(
                                                "egress region rate malformed for {}/{}",
                                                prov, reg
                                            ),
                                        );
                                    }
                                }
                            }
                        }
                    }

                    // Tier 2: provider default.
                    if let Some(d) = block.get("default_usd_per_gb") {
                        let s = match d {
                            Value::String(s) => Some(s.as_str().to_string()),
                            Value::Number(n) => Some(n.to_string()),
                            _ => None,
                        };
                        if let Some(s) = s {
                            if let Ok(rate) = Decimal::from_str(&s) {
                                return EgressRate {
                                    rate_per_gb: rate,
                                    pricing_source: format!(
                                        "egress_catalog:{}:default",
                                        prov
                                    ),
                                    cost_confidence: "estimated".to_string(),
                                };
                            }
                        }
                    }
                }
            }
        }

        // Tier 3: _meta default.
        if let Some(meta) = self.catalog.get("_meta") {
            if let Some(d) = meta.get("default_rate_usd_per_gb") {
                let s = match d {
                    Value::String(s) => Some(s.as_str().to_string()),
                    Value::Number(n) => Some(n.to_string()),
                    _ => None,
                };
                if let Some(s) = s {
                    if let Ok(rate) = Decimal::from_str(&s) {
                        return EgressRate {
                            rate_per_gb: rate,
                            pricing_source: "egress_catalog:default".to_string(),
                            cost_confidence: "estimated".to_string(),
                        };
                    }
                }
            }
            warn_once(
                "meta_default_missing",
                "egress _meta.default_rate_usd_per_gb missing/malformed; \
                 using hardcoded default",
            );
        }

        // Tier 4: hardcoded.
        EgressRate {
            rate_per_gb: hardcoded_default(),
            pricing_source: "egress_catalog:default".to_string(),
            cost_confidence: "estimated".to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    /// Tests touch process-global warn-once state; serialize them so a
    /// `reset_warning_state_for_tests` call in one test doesn't race
    /// another's assertion.
    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    #[test]
    fn test_tier1_region_match_is_computed() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        let r = eng.resolve_rate(Some("aws"), Some("us-east-1"));
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.pricing_source, "egress_catalog:aws:us-east-1");
        assert_eq!(r.cost_confidence, "computed");
    }

    #[test]
    fn test_tier2_provider_known_region_missing_is_estimated() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        let r = eng.resolve_rate(Some("aws"), Some("moon-base-1"));
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.pricing_source, "egress_catalog:aws:default");
        assert_eq!(r.cost_confidence, "estimated");
    }

    #[test]
    fn test_tier3_unknown_provider_falls_to_meta_default() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        let r = eng.resolve_rate(None, None);
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.pricing_source, "egress_catalog:default");
        assert_eq!(r.cost_confidence, "estimated");
    }

    #[test]
    fn test_internal_traffic_is_free_and_exact() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        let r = eng.rate_for_internal();
        assert_eq!(r.rate_per_gb, Decimal::ZERO);
        assert_eq!(r.pricing_source, "egress_catalog:internal");
        assert_eq!(r.cost_confidence, "exact");
    }

    #[test]
    fn test_tier4_missing_catalog_falls_to_hardcoded() {
        let _g = lock();
        reset_warning_state_for_tests();
        let tmp = tempfile::tempdir().unwrap();
        let bogus = tmp.path().join("no.json");
        let eng = EgressPricingEngine::from_path(&bogus);
        let r = eng.resolve_rate(Some("aws"), Some("us-east-1"));
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.cost_confidence, "estimated");
    }

    #[test]
    fn test_tier4_malformed_catalog_falls_to_hardcoded() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::from_str("{not json");
        let r = eng.resolve_rate(Some("aws"), Some("us-east-1"));
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.cost_confidence, "estimated");
    }

    #[test]
    fn test_tier4_meta_default_missing_falls_to_hardcoded() {
        let _g = lock();
        reset_warning_state_for_tests();
        let json = r#"{"_meta": {"version": "x", "currency": "USD"}}"#;
        let eng = EgressPricingEngine::from_str(json);
        let r = eng.resolve_rate(None, None);
        assert_eq!(r.rate_per_gb, Decimal::from_str("0.09").unwrap());
        assert_eq!(r.cost_confidence, "estimated");
    }

    #[test]
    fn test_warn_once_per_failure_mode() {
        let _g = lock();
        reset_warning_state_for_tests();
        // Build two engines with the SAME failure mode; only one warning
        // should be tracked.
        let _e1 = EgressPricingEngine::from_str("{");
        let _e2 = EgressPricingEngine::from_str("{also bad");
        let guard = WARNED_MODES.lock().unwrap();
        assert_eq!(guard.len(), 1);
        assert!(guard.contains("catalog_malformed"));
    }

    #[test]
    fn test_warn_distinct_modes_independently() {
        let _g = lock();
        reset_warning_state_for_tests();
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("missing.json");
        let _e1 = EgressPricingEngine::from_path(&missing);
        let _e2 = EgressPricingEngine::from_str("{");
        let guard = WARNED_MODES.lock().unwrap();
        assert!(guard.contains("catalog_missing"));
        assert!(guard.contains("catalog_malformed"));
    }

    #[test]
    fn test_decimal_no_float_drift() {
        // Critical invariant: the per-GB divisor and rate math must be exact.
        // 1e9 in f64 has no representable drift, but multiplying user rates
        // by a Decimal divisor is the contract.
        let a = Decimal::from_str("0.1093").unwrap()
            * Decimal::from(1_000_000_000_u64);
        assert_eq!(a, Decimal::from_str("109300000.0000").unwrap());

        let b = Decimal::from_str("0.087").unwrap()
            * Decimal::from(12_345_678_u64);
        assert_eq!(b, Decimal::from_str("1074073.986").unwrap());
    }

    #[test]
    fn test_pricing_version_from_meta() {
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        assert_eq!(eng.catalog_version(), "1.0.0");
    }

    #[test]
    fn test_egress_rate_is_clone_eq() {
        // Rust analog of Python's "frozen dataclass" — EgressRate derives
        // Clone/PartialEq/Eq and has no interior mutability.
        let _g = lock();
        reset_warning_state_for_tests();
        let eng = EgressPricingEngine::new();
        let r = eng.resolve_rate(Some("aws"), Some("us-east-1"));
        let r2 = r.clone();
        assert_eq!(r, r2);
    }
}
