//! AWS Lambda cost adapter — compute cost from duration, memory, and region.
//!
//! Pure function [`lambda_cost`] returns a [`LambdaCost`] with the total cost
//! in USD plus a breakdown. Uses bundled pricing JSON; no network I/O.
//!
//! Mirrors the Python adapter `dexcost.adapters.aws_lambda.lambda_cost` (US-043).

use std::collections::BTreeMap;
use std::sync::OnceLock;

use rust_decimal::Decimal;
use serde::Deserialize;

/// Pricing payload bundled with the SDK.
const PRICING_JSON: &str = include_str!("data/aws_lambda_pricing.json");

#[derive(Debug, Deserialize)]
struct RegionPricing {
    duration_per_gb_second: String,
    request_per_invocation: String,
}

#[derive(Debug, Deserialize)]
struct PricingFile {
    regions: BTreeMap<String, RegionPricing>,
    #[serde(default)]
    #[allow(dead_code)]
    _meta: serde_json::Value,
}

fn load_pricing() -> &'static PricingFile {
    static CACHE: OnceLock<PricingFile> = OnceLock::new();
    CACHE.get_or_init(|| {
        serde_json::from_str(PRICING_JSON).expect("bundled aws_lambda_pricing.json must parse")
    })
}

/// Cost breakdown for a single Lambda invocation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LambdaCost {
    /// Total cost (duration + request charge).
    pub cost_usd: Decimal,
    /// Region used for pricing lookup.
    pub region: String,
    /// Execution duration (ms) input.
    pub duration_ms: u64,
    /// Allocated memory (MB) input.
    pub memory_mb: u64,
    /// Computed GB-seconds: `(duration_ms / 1000) * (memory_mb / 1024)`.
    pub gb_seconds: Decimal,
    /// Compute cost (gb_seconds × per-GB-second rate).
    pub duration_cost_usd: Decimal,
    /// Per-invocation request charge.
    pub request_cost_usd: Decimal,
    /// Rate per GB-second used in the lookup.
    pub rate_per_gb_second: Decimal,
}

/// Errors surfaced by [`lambda_cost`].
#[derive(Debug, thiserror::Error)]
pub enum LambdaCostError {
    #[error("memory_mb must be > 0, got 0")]
    ZeroMemory,
    #[error("unknown AWS region '{region}'. Supported regions: {supported}")]
    UnknownRegion { region: String, supported: String },
    #[error("invalid rate '{value}' in bundled pricing data: {source}")]
    InvalidRate {
        value: String,
        #[source]
        source: rust_decimal::Error,
    },
}

/// Returns a sorted list of AWS region codes with bundled pricing data.
pub fn supported_regions() -> Vec<String> {
    load_pricing().regions.keys().cloned().collect()
}

/// Calculate the cost of a single AWS Lambda invocation.
///
/// This is a **pure function** — no I/O, no side effects. It uses the bundled
/// `aws_lambda_pricing.json` for rates.
///
/// # Arguments
///
/// * `duration_ms` — Execution duration in milliseconds.
/// * `memory_mb` — Allocated memory in MB. Must be > 0.
/// * `region` — AWS region code (e.g. `"us-east-1"`).
pub fn lambda_cost(
    duration_ms: u64,
    memory_mb: u64,
    region: &str,
) -> Result<LambdaCost, LambdaCostError> {
    if memory_mb == 0 {
        return Err(LambdaCostError::ZeroMemory);
    }

    let pricing = load_pricing();
    let region_pricing = pricing.regions.get(region).ok_or_else(|| {
        let supported = pricing
            .regions
            .keys()
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        LambdaCostError::UnknownRegion {
            region: region.to_string(),
            supported,
        }
    })?;

    let rate_per_gb_second = parse_decimal(&region_pricing.duration_per_gb_second)?;
    let request_charge = parse_decimal(&region_pricing.request_per_invocation)?;

    let duration_seconds = Decimal::from(duration_ms) / Decimal::from(1000u64);
    let memory_gb = Decimal::from(memory_mb) / Decimal::from(1024u64);
    let gb_seconds = duration_seconds * memory_gb;

    let duration_cost = gb_seconds * rate_per_gb_second;
    let cost_usd = duration_cost + request_charge;

    Ok(LambdaCost {
        cost_usd,
        region: region.to_string(),
        duration_ms,
        memory_mb,
        gb_seconds,
        duration_cost_usd: duration_cost,
        request_cost_usd: request_charge,
        rate_per_gb_second,
    })
}

fn parse_decimal(value: &str) -> Result<Decimal, LambdaCostError> {
    value
        .parse()
        .map_err(|source| LambdaCostError::InvalidRate {
            value: value.to_string(),
            source,
        })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn supported_regions_includes_us_east_1() {
        let regions = supported_regions();
        assert!(regions.contains(&"us-east-1".to_string()));
    }

    #[test]
    fn rejects_zero_memory() {
        let err = lambda_cost(100, 0, "us-east-1").unwrap_err();
        assert!(matches!(err, LambdaCostError::ZeroMemory));
    }

    #[test]
    fn rejects_unknown_region() {
        let err = lambda_cost(100, 128, "moon-base-1").unwrap_err();
        match err {
            LambdaCostError::UnknownRegion { region, supported } => {
                assert_eq!(region, "moon-base-1");
                assert!(!supported.is_empty());
            }
            other => panic!("expected UnknownRegion, got {:?}", other),
        }
    }

    #[test]
    fn computes_breakdown_for_us_east_1() {
        // 1000 ms @ 128 MB in us-east-1
        // gb_seconds = 1.0 * (128/1024) = 0.125
        // duration_cost = 0.125 * 0.0000166667 ≈ 0.00000208333...
        // request_cost = 0.0000002
        // total ≈ 0.00000228333...
        let result = lambda_cost(1000, 128, "us-east-1").expect("should compute");
        assert_eq!(result.region, "us-east-1");
        assert_eq!(result.duration_ms, 1000);
        assert_eq!(result.memory_mb, 128);
        assert_eq!(result.gb_seconds, dec!(0.125));
        assert_eq!(result.rate_per_gb_second, dec!(0.0000166667));
        assert_eq!(result.request_cost_usd, dec!(0.0000002));
        assert!(result.cost_usd > Decimal::ZERO);
        // duration_cost + request_cost == total
        assert_eq!(
            result.cost_usd,
            result.duration_cost_usd + result.request_cost_usd
        );
    }

    #[test]
    fn zero_duration_still_charges_request() {
        let result = lambda_cost(0, 128, "us-east-1").expect("zero ms is valid");
        assert_eq!(result.gb_seconds, Decimal::ZERO);
        assert_eq!(result.duration_cost_usd, Decimal::ZERO);
        assert_eq!(result.cost_usd, result.request_cost_usd);
    }

    #[test]
    fn cost_is_deterministic() {
        let a = lambda_cost(2500, 256, "eu-west-1").unwrap();
        let b = lambda_cost(2500, 256, "eu-west-1").unwrap();
        assert_eq!(a, b);
    }
}
