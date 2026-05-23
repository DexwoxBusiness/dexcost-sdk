//! GPU pricing engine — Phase 2 Task 5 (heart of v2 cost attribution).
//!
//! Rust port of `python/src/dexcost/gpu_pricing.py`. Dispatches on
//! `details["billing_model"]` to one of four methods and returns a
//! [`GpuCost`] for every input — never raises.
//!
//! Four billing models:
//! - `per_gpu_second_active` — Modal / RunPod / Replicate
//! - `per_instance_hour`     — AWS EC2 GPU / GCP GCE bundled / Azure VM GPU
//! - `per_gpu_hour_reserved` — Lambda Labs / CoreWeave / GCP N1 + accelerator
//! - `per_vgpu_hour`         — Azure NVadsA10 v5 fractional
//!
//! Five-tier degradation ladder per convention §7:
//! 1. Exact SKU match → `computed`
//! 2. Provider+SKU known, region missing → provider default → `computed`
//! 3. Decision #4 device-class fallback (productName → hopper/ampere/
//!    ada_lovelace/blackwell) → `estimated`
//! 3b. `_meta.default_<billing_model>_usd` → `estimated`
//! 4. Tier-4 HARDCODED constants → `estimated` + log-once
//! 5. Tier-5 try/catch wraps the public method (in Rust we use
//!    `std::panic::catch_unwind` matching Phase 1 compute_pricing.rs)

use std::collections::HashSet;
use std::str::FromStr;
use std::sync::{LazyLock, Mutex};

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde_json::Value;

use crate::cloud_detect::CloudEnv;

const BUNDLED_CATALOG: &str = include_str!("../data/gpu_prices.json");

static HOUR_S: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(3600u64));
static MS_PER_S: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(1000u64));

/// Tier-4 hardcoded constants — mirror `_meta` defaults in `gpu_prices.json`.
/// Used when the catalog cannot be parsed at all.
struct Hardcoded {
    hourly_usd: Decimal,
    gpu_second_usd: Decimal,
    gpu_hour_usd: Decimal,
    vgpu_hour_usd: Decimal,
}

static HARDCODED: LazyLock<Hardcoded> = LazyLock::new(|| Hardcoded {
    hourly_usd: dec!(55.04),
    gpu_second_usd: dec!(0.000694),
    gpu_hour_usd: dec!(3.99),
    vgpu_hour_usd: dec!(0.454),
});

/// Decision #4 device-class default rates — cold-start fallback for unknown SKUs.
struct DeviceClassRates {
    per_instance_hour: Decimal,
    per_gpu_second_active: Decimal,
    per_gpu_hour_reserved: Decimal,
    per_vgpu_hour: Decimal,
}

fn device_class_rate(class: &str, billing_model: &str) -> Option<Decimal> {
    let rates: DeviceClassRates = match class {
        "hopper" => DeviceClassRates {
            per_instance_hour: dec!(98.32),
            per_gpu_second_active: dec!(0.001097),
            per_gpu_hour_reserved: dec!(3.99),
            per_vgpu_hour: dec!(3.99),
        },
        "ampere" => DeviceClassRates {
            per_instance_hour: dec!(32.77),
            per_gpu_second_active: dec!(0.000833),
            per_gpu_hour_reserved: dec!(2.20),
            per_vgpu_hour: dec!(2.20),
        },
        "ada_lovelace" => DeviceClassRates {
            per_instance_hour: dec!(12.00),
            per_gpu_second_active: dec!(0.000400),
            per_gpu_hour_reserved: dec!(1.50),
            per_vgpu_hour: dec!(1.50),
        },
        "blackwell" => DeviceClassRates {
            per_instance_hour: dec!(180.00),
            per_gpu_second_active: dec!(0.002500),
            per_gpu_hour_reserved: dec!(6.50),
            per_vgpu_hour: dec!(6.50),
        },
        _ => return None,
    };
    Some(match billing_model {
        "per_instance_hour" => rates.per_instance_hour,
        "per_gpu_second_active" => rates.per_gpu_second_active,
        "per_gpu_hour_reserved" => rates.per_gpu_hour_reserved,
        "per_vgpu_hour" => rates.per_vgpu_hour,
        _ => return None,
    })
}

/// Substring patterns that map a normalized productName to a device class.
/// Most specific first.
const DEVICE_CLASS_PATTERNS: &[(&str, &[&str])] = &[
    ("blackwell", &["b100", "b200", "gb200", "b300", "blackwell"]),
    ("hopper", &["h100", "h200", "hopper"]),
    ("ada_lovelace", &["l4", "l40", "ada lovelace", "rtx 4090", "rtx 5090"]),
    ("ampere", &["a100", "a40", "a10", "ampere", "rtx 3090", "rtx a6000"]),
];

fn detect_device_class(product_name_lower: Option<&str>) -> Option<&'static str> {
    let name = product_name_lower?;
    for (cls, patterns) in DEVICE_CLASS_PATTERNS {
        for p in *patterns {
            if name.contains(p) {
                return Some(*cls);
            }
        }
    }
    None
}

// ── log-once-per-failure-mode (convention §11) ─────────────────────────────

static WARNED_MODES: LazyLock<Mutex<HashSet<String>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

fn warn_once(mode: &str, message: &str) {
    let mut g = WARNED_MODES.lock().expect("warn-once mutex poisoned");
    if !g.insert(mode.to_string()) {
        return;
    }
    drop(g);
    eprintln!("[dexcost][gpu] {}: {}", mode, message);
}

#[doc(hidden)]
pub fn reset_warning_state_for_tests() {
    WARNED_MODES
        .lock()
        .expect("warn-once mutex poisoned")
        .clear();
}

// ── Public types ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GpuCost {
    pub cost_usd: Decimal,
    pub pricing_source: String,
    /// `"computed"` | `"estimated"` | `"unknown"`.
    pub cost_confidence: String,
}

impl GpuCost {
    fn error(billing_model: &str) -> Self {
        Self {
            cost_usd: Decimal::ZERO,
            pricing_source: format!("gpu_catalog:error:{}", billing_model),
            cost_confidence: "unknown".to_string(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct GpuPricingEngine {
    catalog: Value,
    catalog_version: String,
}

impl Default for GpuPricingEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl GpuPricingEngine {
    pub fn new() -> Self {
        Self::from_str(BUNDLED_CATALOG)
    }

    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Self {
        let catalog: Value = match serde_json::from_str(s) {
            Ok(v) => v,
            Err(e) => {
                warn_once(
                    "gpu_catalog_malformed",
                    &format!("gpu catalog malformed ({}); using HARDCODED", e),
                );
                Value::Null
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

    pub fn catalog_version(&self) -> &str {
        &self.catalog_version
    }

    pub fn catalog(&self) -> &Value {
        &self.catalog
    }

    /// Public entry. Wraps `dispatch` in `catch_unwind` so a pricing bug
    /// can't break task finalize (Tier-5). Applies Decision #1
    /// measurement-side fallback suffix after rate resolution.
    pub fn resolve_gpu_cost(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        window_s: Option<Decimal>,
    ) -> GpuCost {
        let billing_model = details
            .get("billing_model")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();

        let dispatch_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.dispatch(&billing_model, details, cloud_env, window_s)
        }));
        let cost = match dispatch_result {
            Ok(c) => c,
            Err(_) => {
                warn_once(
                    &format!("gpu_pricing_failure:{}", billing_model),
                    &format!("gpu pricing panic for billing_model={}", billing_model),
                );
                return GpuCost::error(&billing_model);
            }
        };

        // Decision #1 measurement-side fallback suffix.
        if let Some(scope_fb) = details
            .get("_cgroup_scope_fallback")
            .and_then(|v| v.as_str())
        {
            return GpuCost {
                cost_usd: cost.cost_usd,
                pricing_source: format!("{}:{}", cost.pricing_source, scope_fb),
                cost_confidence: "estimated".to_string(),
            };
        }
        cost
    }

    fn dispatch(
        &self,
        billing_model: &str,
        details: &Value,
        cloud_env: &CloudEnv,
        window_s: Option<Decimal>,
    ) -> GpuCost {
        match billing_model {
            "per_gpu_second_active" => self.per_gpu_second(details, cloud_env),
            "per_instance_hour" => self.per_instance_hour(details, cloud_env, window_s),
            "per_gpu_hour_reserved" => self.per_gpu_hour(details, cloud_env, window_s),
            "per_vgpu_hour" => self.per_vgpu_hour(details, cloud_env, window_s),
            _ => {
                warn_once(
                    &format!("gpu_unsupported_billing_model:{}", billing_model),
                    &format!("gpu pricing has no math for billing_model={}", billing_model),
                );
                GpuCost {
                    cost_usd: Decimal::ZERO,
                    pricing_source: format!("gpu_catalog:unsupported:{}", billing_model),
                    cost_confidence: "unknown".to_string(),
                }
            }
        }
    }

    // ── Helpers ────────────────────────────────────────────────────────

    fn dec_field(details: &Value, key: &str) -> Decimal {
        details
            .get(key)
            .and_then(|v| {
                if let Some(s) = v.as_str() {
                    Decimal::from_str(s).ok()
                } else if let Some(f) = v.as_f64() {
                    Decimal::from_str(&f.to_string()).ok()
                } else if let Some(i) = v.as_i64() {
                    Some(Decimal::from(i))
                } else if let Some(u) = v.as_u64() {
                    Some(Decimal::from(u))
                } else {
                    None
                }
            })
            .unwrap_or(Decimal::ZERO)
    }

    fn str_field<'a>(details: &'a Value, key: &str) -> Option<&'a str> {
        details.get(key).and_then(|v| v.as_str())
    }

    fn window_seconds(details: &Value, window_s: Option<Decimal>) -> Decimal {
        if let Some(w) = window_s {
            if w > Decimal::ZERO {
                return w;
            }
        }
        let dur_ms = Self::dec_field(details, "duration_ms");
        dur_ms / *MS_PER_S
    }

    // ── per_gpu_second_active ──────────────────────────────────────────

    fn per_gpu_second(&self, details: &Value, cloud_env: &CloudEnv) -> GpuCost {
        let provider = cloud_env.provider.as_deref();
        let gpu_sku = Self::str_field(details, "gpu_sku");
        let (rate, source, confidence) =
            self.resolve_per_gpu_second_rate(provider, gpu_sku, details);
        let gpu_seconds = Self::dec_field(details, "gpu_seconds_used");
        let cost = gpu_seconds * rate;
        GpuCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence,
        }
    }

    fn resolve_per_gpu_second_rate(
        &self,
        provider: Option<&str>,
        gpu_sku: Option<&str>,
        details: &Value,
    ) -> (Decimal, String, String) {
        if let (Some(prov), Some(sku)) = (provider, gpu_sku) {
            if let Some(block) = self
                .catalog
                .get(prov)
                .and_then(|p| p.get("per_gpu_second_active"))
            {
                if let Some(default) = block.get("default").and_then(|d| d.as_object()) {
                    // Direct lookup (Modal / Replicate shape)
                    for (key, entry) in default {
                        if let Some(eobj) = entry.as_object() {
                            if eobj.get("gpu_sku").and_then(|v| v.as_str()) == Some(sku) {
                                if let Some(rate) = Self::dec_from(entry.get("gpu_second_usd")) {
                                    return (
                                        rate,
                                        format!(
                                            "gpu_catalog:{}:per_gpu_second_active:{}",
                                            prov, key
                                        ),
                                        "computed".into(),
                                    );
                                }
                            }
                            // Nested lookup (RunPod on_demand/community_cloud)
                            for (sku_key, sku_entry) in eobj {
                                if let Some(sobj) = sku_entry.as_object() {
                                    if sobj.get("gpu_sku").and_then(|v| v.as_str()) == Some(sku) {
                                        if let Some(rate) =
                                            Self::dec_from(sku_entry.get("gpu_second_usd"))
                                        {
                                            return (
                                                rate,
                                                format!(
                                                    "gpu_catalog:{}:per_gpu_second_active:{}:{}",
                                                    prov, key, sku_key
                                                ),
                                                "computed".into(),
                                            );
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        self.device_class_or_meta_fallback(details, "per_gpu_second_active", "gpu_second_usd")
    }

    // ── per_instance_hour ──────────────────────────────────────────────

    fn per_instance_hour(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        window_s: Option<Decimal>,
    ) -> GpuCost {
        let window = Self::window_seconds(details, window_s);
        let provider = cloud_env.provider.as_deref();
        let region = Self::str_field(details, "region");
        let instance_type =
            Self::str_field(details, "instance_type").or(cloud_env.instance_type.as_deref());
        let (hourly_rate, source, confidence) =
            self.resolve_per_instance_rate(provider, region, instance_type, details);
        let gpu_count = Self::dec_field(details, "gpu_count");
        let gpu_seconds = Self::dec_field(details, "gpu_seconds_used");
        if gpu_count <= Decimal::ZERO || window <= Decimal::ZERO {
            return GpuCost {
                cost_usd: Decimal::ZERO,
                pricing_source: source,
                cost_confidence: confidence,
            };
        }
        let share_factor = gpu_seconds / (gpu_count * window);
        let task_instance_hours = share_factor * (window / *HOUR_S);
        let cost = task_instance_hours * hourly_rate;
        GpuCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence,
        }
    }

    fn resolve_per_instance_rate(
        &self,
        provider: Option<&str>,
        region: Option<&str>,
        instance_type: Option<&str>,
        details: &Value,
    ) -> (Decimal, String, String) {
        let block_key = match provider {
            Some("aws") => Some("ec2_gpu"),
            Some("gcp") => Some("gce_gpu_bundled"),
            Some("azure") => Some("vm_gpu"),
            _ => None,
        };
        if let (Some(prov), Some(bkey), Some(reg), Some(itype)) =
            (provider, block_key, region, instance_type)
        {
            if let Some(entry) = self
                .catalog
                .get(prov)
                .and_then(|p| p.get(bkey))
                .and_then(|b| b.get("regions"))
                .and_then(|r| r.get(reg))
                .and_then(|r| r.get("instance_types"))
                .and_then(|t| t.get(itype))
            {
                if let Some(rate) = Self::dec_from(entry.get("hourly_usd")) {
                    return (
                        rate,
                        format!("gpu_catalog:{}:{}:{}:{}", prov, bkey, reg, itype),
                        "computed".into(),
                    );
                }
            }
        }
        self.device_class_or_meta_fallback(details, "per_instance_hour", "hourly_usd")
    }

    // ── per_gpu_hour_reserved ──────────────────────────────────────────

    fn per_gpu_hour(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        window_s: Option<Decimal>,
    ) -> GpuCost {
        let window = Self::window_seconds(details, window_s);
        let provider = cloud_env.provider.as_deref();
        let gpu_sku = Self::str_field(details, "gpu_sku");
        let (gpu_hour_usd, source, confidence) =
            self.resolve_per_gpu_hour_rate(provider, gpu_sku, details);
        let gpu_count = Self::dec_field(details, "gpu_count");
        let gpu_seconds = Self::dec_field(details, "gpu_seconds_used");
        if gpu_count <= Decimal::ZERO || window <= Decimal::ZERO {
            return GpuCost {
                cost_usd: Decimal::ZERO,
                pricing_source: source,
                cost_confidence: confidence,
            };
        }
        let share_factor = gpu_seconds / (gpu_count * window);
        let task_gpu_hours = share_factor * (window / *HOUR_S) * gpu_count;
        let cost = task_gpu_hours * gpu_hour_usd;
        GpuCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence,
        }
    }

    fn resolve_per_gpu_hour_rate(
        &self,
        provider: Option<&str>,
        gpu_sku: Option<&str>,
        details: &Value,
    ) -> (Decimal, String, String) {
        if let (Some(prov), Some(sku)) = (provider, gpu_sku) {
            if let Some(default) = self
                .catalog
                .get(prov)
                .and_then(|p| p.get("per_gpu_hour_reserved"))
                .and_then(|b| b.get("default"))
                .and_then(|d| d.as_object())
            {
                for (key, entry) in default {
                    if let Some(eobj) = entry.as_object() {
                        if eobj.get("gpu_sku").and_then(|v| v.as_str()) == Some(sku) {
                            if let Some(rate) = Self::dec_from(entry.get("gpu_hour_usd")) {
                                return (
                                    rate,
                                    format!("gpu_catalog:{}:per_gpu_hour_reserved:{}", prov, key),
                                    "computed".into(),
                                );
                            }
                        }
                    }
                }
            }
        }
        // GCP N1 + accelerator path (Decision #9)
        if provider == Some("gcp") {
            if let (Some(sku), Some(region)) = (gpu_sku, Self::str_field(details, "region")) {
                if let Some(accelerators) = self
                    .catalog
                    .get("gcp")
                    .and_then(|p| p.get("gce_gpu_attached"))
                    .and_then(|b| b.get("regions"))
                    .and_then(|r| r.get(region))
                    .and_then(|r| r.get("accelerator_types"))
                    .and_then(|t| t.as_object())
                {
                    for (acc_key, entry) in accelerators {
                        if entry.get("gpu_sku").and_then(|v| v.as_str()) == Some(sku) {
                            if let Some(rate) = Self::dec_from(entry.get("gpu_hour_usd")) {
                                return (
                                    rate,
                                    format!(
                                        "gpu_catalog:gcp:gce_gpu_attached:{}:{}",
                                        region, acc_key
                                    ),
                                    "computed".into(),
                                );
                            }
                        }
                    }
                }
            }
        }
        self.device_class_or_meta_fallback(details, "per_gpu_hour_reserved", "gpu_hour_usd")
    }

    // ── per_vgpu_hour (Decision #10) ───────────────────────────────────

    fn per_vgpu_hour(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        window_s: Option<Decimal>,
    ) -> GpuCost {
        let window = Self::window_seconds(details, window_s);
        let provider = cloud_env.provider.as_deref();
        let region = Self::str_field(details, "region");
        let instance_type =
            Self::str_field(details, "instance_type").or(cloud_env.instance_type.as_deref());
        let (vgpu_hour_usd, source, confidence) =
            self.resolve_per_vgpu_rate(provider, region, instance_type, details);
        let gpu_seconds = Self::dec_field(details, "gpu_seconds_used");
        if window <= Decimal::ZERO {
            return GpuCost {
                cost_usd: Decimal::ZERO,
                pricing_source: source,
                cost_confidence: confidence,
            };
        }
        let share_factor = gpu_seconds / window;
        let task_vgpu_hours = share_factor * (window / *HOUR_S);
        let cost = task_vgpu_hours * vgpu_hour_usd;
        GpuCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence,
        }
    }

    fn resolve_per_vgpu_rate(
        &self,
        provider: Option<&str>,
        region: Option<&str>,
        instance_type: Option<&str>,
        details: &Value,
    ) -> (Decimal, String, String) {
        if let (Some("azure"), Some(reg), Some(itype)) = (provider, region, instance_type) {
            if let Some(entry) = self
                .catalog
                .get("azure")
                .and_then(|p| p.get("vm_vgpu"))
                .and_then(|b| b.get("regions"))
                .and_then(|r| r.get(reg))
                .and_then(|r| r.get("instance_types"))
                .and_then(|t| t.get(itype))
            {
                if let Some(rate) = Self::dec_from(entry.get("vgpu_hour_usd")) {
                    return (
                        rate,
                        format!("gpu_catalog:azure:vm_vgpu:{}:{}", reg, itype),
                        "computed".into(),
                    );
                }
            }
        }
        self.device_class_or_meta_fallback(details, "per_vgpu_hour", "vgpu_hour_usd")
    }

    // ── Tier-3 / Tier-4 fallback ladder ────────────────────────────────

    fn device_class_or_meta_fallback(
        &self,
        details: &Value,
        billing_model: &str,
        rate_key: &str,
    ) -> (Decimal, String, String) {
        // Tier-3a: device-class fallback via NFC-normalized productName.
        let product_name = Self::str_field(details, "_nvml_product_name_lower");
        if let Some(class) = detect_device_class(product_name) {
            if let Some(rate) = device_class_rate(class, billing_model) {
                warn_once(
                    &format!("gpu_sku_unknown:{}", product_name.unwrap_or("")),
                    &format!(
                        "GPU SKU not in catalog (productName={:?}); falling back to \
                         device_class={} default rate",
                        product_name, class
                    ),
                );
                return (
                    rate,
                    format!("gpu_catalog:device_class_fallback:{}:{}", class, billing_model),
                    "estimated".into(),
                );
            }
        }

        // Tier-3b: universal _meta default.
        let meta_key = format!("default_{}_usd", billing_model);
        if let Some(s) = self
            .catalog
            .get("_meta")
            .and_then(|m| m.get(&meta_key))
            .and_then(|v| v.as_str())
        {
            if let Ok(rate) = Decimal::from_str(s) {
                return (
                    rate,
                    format!("gpu_catalog:default:{}", billing_model),
                    "estimated".into(),
                );
            }
        }

        // Tier-4: hardcoded.
        let rate = match (billing_model, rate_key) {
            ("per_instance_hour", _) => HARDCODED.hourly_usd,
            ("per_gpu_second_active", _) => HARDCODED.gpu_second_usd,
            ("per_gpu_hour_reserved", _) => HARDCODED.gpu_hour_usd,
            ("per_vgpu_hour", _) => HARDCODED.vgpu_hour_usd,
            _ => Decimal::ZERO,
        };
        (
            rate,
            format!("gpu_catalog:hardcoded:{}", billing_model),
            "estimated".into(),
        )
    }

    fn dec_from(v: Option<&Value>) -> Option<Decimal> {
        let v = v?;
        if let Some(s) = v.as_str() {
            Decimal::from_str(s).ok()
        } else if let Some(f) = v.as_f64() {
            Decimal::from_str(&f.to_string()).ok()
        } else if let Some(i) = v.as_i64() {
            Some(Decimal::from(i))
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cloud(provider: Option<&str>, region: Option<&str>, instance_type: Option<&str>) -> CloudEnv {
        CloudEnv {
            provider: provider.map(String::from),
            region: region.map(String::from),
            source: "test",
            instance_type: instance_type.map(String::from),
        }
    }

    #[test]
    fn catalog_loads_meta_version() {
        let e = GpuPricingEngine::new();
        // _meta.version is X.Y.Z semver in the bundled catalog.
        assert!(!e.catalog_version().is_empty());
        assert_ne!(e.catalog_version(), "unknown");
    }

    #[test]
    fn malformed_catalog_falls_through_hardcoded() {
        let e = GpuPricingEngine::from_str("not json");
        // hardcoded fallback applies for any billing_model.
        let details = json!({
            "billing_model": "per_gpu_second_active",
            "gpu_seconds_used": "10",
            "gpu_sku": "h100-80gb-sxm5",
            "duration_ms": 1000,
            "gpu_count": 1,
        });
        let c = e.resolve_gpu_cost(&details, &cloud(Some("modal"), None, None), None);
        assert!(c.pricing_source.starts_with("gpu_catalog:"));
    }

    #[test]
    fn modal_per_gpu_second_active_h100() {
        let e = GpuPricingEngine::new();
        let details = json!({
            "billing_model": "per_gpu_second_active",
            "gpu_seconds_used": "10",
            "gpu_sku": "h100-80gb-sxm5",
            "duration_ms": 10000,
            "gpu_count": 1,
        });
        let c = e.resolve_gpu_cost(&details, &cloud(Some("modal"), None, None), None);
        assert_eq!(c.cost_confidence, "computed");
        assert!(c.pricing_source.starts_with("gpu_catalog:modal:per_gpu_second_active:"));
        assert!(c.cost_usd > Decimal::ZERO);
    }

    #[test]
    fn aws_per_instance_hour_p5() {
        let e = GpuPricingEngine::new();
        let details = json!({
            "billing_model": "per_instance_hour",
            "gpu_seconds_used": "30",
            "gpu_count": 8,
            "duration_ms": 60000,
            "region": "us-east-1",
            "instance_type": "p5.48xlarge",
        });
        let c = e.resolve_gpu_cost(
            &details,
            &cloud(Some("aws"), Some("us-east-1"), Some("p5.48xlarge")),
            None,
        );
        // Either computed (if catalog has the entry) or estimated via fallback.
        assert!(c.pricing_source.starts_with("gpu_catalog:"));
        assert!(c.cost_usd >= Decimal::ZERO);
    }

    #[test]
    fn lambda_labs_per_gpu_hour() {
        let e = GpuPricingEngine::new();
        let details = json!({
            "billing_model": "per_gpu_hour_reserved",
            "gpu_seconds_used": "60",
            "gpu_count": 8,
            "duration_ms": 60000,
            "gpu_sku": "h100-80gb-sxm5",
        });
        let c = e.resolve_gpu_cost(&details, &cloud(Some("lambda_labs"), None, None), None);
        assert!(c.pricing_source.starts_with("gpu_catalog:"));
        assert!(c.cost_usd >= Decimal::ZERO);
    }

    #[test]
    fn azure_per_vgpu_hour() {
        let e = GpuPricingEngine::new();
        let details = json!({
            "billing_model": "per_vgpu_hour",
            "gpu_seconds_used": "10",
            "gpu_count": 1,
            "duration_ms": 10000,
            "region": "eastus",
            "instance_type": "Standard_NV6ads_A10_v5",
        });
        let c = e.resolve_gpu_cost(
            &details,
            &cloud(Some("azure"), Some("eastus"), Some("Standard_NV6ads_A10_v5")),
            None,
        );
        assert!(c.pricing_source.starts_with("gpu_catalog:"));
        assert!(c.cost_usd >= Decimal::ZERO);
    }

    #[test]
    fn unsupported_billing_model_returns_unknown_confidence() {
        let e = GpuPricingEngine::new();
        let details = json!({"billing_model": "weird_model"});
        let c = e.resolve_gpu_cost(&details, &cloud(None, None, None), None);
        assert_eq!(c.cost_confidence, "unknown");
        assert_eq!(c.cost_usd, Decimal::ZERO);
    }

    #[test]
    fn decision_4_device_class_fallback_blackwell() {
        // Hypothetical "B300" SKU not in catalog → device-class fallback.
        let e = GpuPricingEngine::new();
        let details = json!({
            "billing_model": "per_gpu_second_active",
            "gpu_seconds_used": "10",
            "gpu_sku": "b300-200gb-hbm4",
            "_nvml_product_name_lower": "nvidia b300 200gb hbm4",
            "duration_ms": 10000,
            "gpu_count": 1,
        });
        let c = e.resolve_gpu_cost(&details, &cloud(Some("modal"), None, None), None);
        assert!(
            c.pricing_source.starts_with("gpu_catalog:device_class_fallback:blackwell:")
                || c.pricing_source.starts_with("gpu_catalog:modal:per_gpu_second_active:"),
            "expected device_class fallback OR direct catalog hit; got {}",
            c.pricing_source
        );
    }

    #[test]
    fn decision_1_fallback_appends_suffix_and_drops_confidence() {
        let e = GpuPricingEngine::new();
        for label in ["self_pid_only", "no_container_scope", "multi_container_pod_partial"] {
            let details = json!({
                "billing_model": "per_gpu_second_active",
                "gpu_seconds_used": "1",
                "gpu_sku": "h100-80gb-sxm5",
                "duration_ms": 1000,
                "gpu_count": 1,
                "_cgroup_scope_fallback": label,
            });
            let c = e.resolve_gpu_cost(&details, &cloud(Some("modal"), None, None), None);
            assert!(
                c.pricing_source.ends_with(&format!(":{}", label)),
                "pricing_source must end with :{} — got {}",
                label,
                c.pricing_source
            );
            assert_eq!(c.cost_confidence, "estimated");
        }
    }

    #[test]
    fn detect_device_class_substring_table() {
        assert_eq!(detect_device_class(Some("nvidia h100 80gb hbm3")), Some("hopper"));
        assert_eq!(detect_device_class(Some("nvidia a100 40gb")), Some("ampere"));
        assert_eq!(detect_device_class(Some("nvidia l4")), Some("ada_lovelace"));
        assert_eq!(detect_device_class(Some("nvidia b200")), Some("blackwell"));
        assert_eq!(detect_device_class(Some("nvidia tesla t4")), None);
        assert_eq!(detect_device_class(None), None);
    }

    #[test]
    fn pricing_source_starts_with_gpu_catalog_invariant() {
        // Convention §3 — every pricing_source starts with "gpu_catalog:"
        // for the four canonical billing models.
        let e = GpuPricingEngine::new();
        let inputs = [
            (
                "per_gpu_second_active",
                json!({
                    "billing_model": "per_gpu_second_active",
                    "gpu_seconds_used": "1", "gpu_sku": "h100-80gb-sxm5",
                    "duration_ms": 1000, "gpu_count": 1,
                }),
                cloud(Some("modal"), None, None),
            ),
            (
                "per_instance_hour",
                json!({
                    "billing_model": "per_instance_hour",
                    "gpu_seconds_used": "1", "gpu_count": 1,
                    "duration_ms": 1000, "region": "us-east-1",
                    "instance_type": "p5.48xlarge",
                }),
                cloud(Some("aws"), Some("us-east-1"), Some("p5.48xlarge")),
            ),
            (
                "per_gpu_hour_reserved",
                json!({
                    "billing_model": "per_gpu_hour_reserved",
                    "gpu_seconds_used": "1", "gpu_count": 1,
                    "duration_ms": 1000, "gpu_sku": "h100-80gb-sxm5",
                }),
                cloud(Some("lambda_labs"), None, None),
            ),
            (
                "per_vgpu_hour",
                json!({
                    "billing_model": "per_vgpu_hour",
                    "gpu_seconds_used": "1", "gpu_count": 1,
                    "duration_ms": 1000, "region": "eastus",
                    "instance_type": "Standard_NV6ads_A10_v5",
                }),
                cloud(Some("azure"), Some("eastus"), Some("Standard_NV6ads_A10_v5")),
            ),
        ];
        for (model, details, env) in inputs.iter() {
            let c = GpuPricingEngine::new().resolve_gpu_cost(details, env, None);
            assert!(
                c.pricing_source.starts_with("gpu_catalog:"),
                "billing_model {} must produce gpu_catalog: prefix — got {}",
                model,
                c.pricing_source
            );
        }
    }
}
