//! Compute pricing engine — resolves per-billing-model compute costs from the
//! bundled `data/compute_prices.json` catalog.
//!
//! This is the Rust port of `python/src/dexcost/compute_pricing.py`. The
//! catalog is loaded once via `include_str!`; failures fall through to the
//! HARDCODED Tier-4 defaults below. All math uses `rust_decimal::Decimal`.
//!
//! Dispatch on `details["billing_model"]`:
//!   - `lambda`               → request+gb-second
//!   - `fargate`              → vcpu-second+gib-second (BINARY GiB — Decision #6 pin)
//!   - `cloud_run_request`    → request+vcpu-second+gib-second (BINARY GiB)
//!   - `cloud_run_instance`   → vcpu-second+gib-second (BINARY GiB)
//!   - `cloud_functions`      → request+vcpu-second+gib-second (BINARY GiB)
//!   - `azure_functions`      → execution+gb-second (DECIMAL GB)
//!   - `vercel_fluid`         → cpu-hour+memory-gb-hour
//!   - `ec2` / `gce` / `azure_vm` (IaaS share model — canonical, no `_share` suffix)
//!                            → SKU hourly_usd / vcpu_count × duration share
//!   - `k8s_pod`              → vcpu-hour × pod limits × duration share

use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::{LazyLock, Mutex};

use rust_decimal::Decimal;
use serde_json::Value;

use crate::cloud_detect::CloudEnv;

const BUNDLED_CATALOG: &str = include_str!("../data/compute_prices.json");

pub static GB_DECIMAL: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(1_000_000_000u64));
/// BINARY GiB divisor — load-bearing for Fargate / Cloud Run / Cloud Functions.
/// Using the decimal GB divisor causes ~4.86% silent over-attribution.
pub static GIB_BINARY: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(1024u64 * 1024 * 1024));
pub static HOUR_S: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(3600u64));
pub static MS_PER_S: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(1000u64));

/// Tier-4 fallback constants — mirror the `_meta` defaults in the bundled
/// catalog (last refreshed in commit `fb5d0a0` with live AWS/Azure/GCP/Vercel
/// verification). Used when the catalog cannot be parsed at all.
pub static HARDCODED: LazyLock<HashMap<&'static str, Decimal>> = LazyLock::new(|| {
    let mut m: HashMap<&'static str, Decimal> = HashMap::new();
    let d = |s: &str| Decimal::from_str(s).expect("HARDCODED Decimal parses");
    m.insert("default_lambda_request_usd", d("0.0000002"));
    m.insert("default_lambda_gb_second_usd", d("0.0000166667"));
    m.insert("default_fargate_vcpu_second_usd", d("0.0000112444"));
    m.insert("default_fargate_gib_second_usd", d("0.0000012347"));
    m.insert("default_cloud_run_request_usd", d("0.0000004"));
    m.insert("default_cloud_run_vcpu_second_usd", d("0.000024"));
    m.insert("default_cloud_run_gib_second_usd", d("0.0000025"));
    m.insert("default_azure_functions_execution_usd", d("0.0000002"));
    m.insert("default_azure_functions_gb_second_usd", d("0.000016"));
    m.insert("default_vercel_cpu_hour_usd", d("0.128"));
    m.insert("default_vercel_memory_gb_hour_usd", d("0.0106"));
    m.insert("default_ec2_vcpu_hour_usd", d("0.0464"));
    m.insert("default_k8s_pod_vcpu_hour_usd", d("0.0464"));
    m
});

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
    WARNED_MODES
        .lock()
        .expect("warn-once mutex poisoned")
        .clear();
}

/// Result of a compute-cost resolution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComputeCost {
    pub cost_usd: Decimal,
    pub pricing_source: String,
    /// `"computed"` or `"estimated"`.
    pub cost_confidence: String,
}

impl ComputeCost {
    pub fn zero_unknown() -> Self {
        Self {
            cost_usd: Decimal::ZERO,
            pricing_source: "compute_catalog:unknown".to_string(),
            cost_confidence: "estimated".to_string(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ComputePricingEngine {
    catalog: Value,
    catalog_version: String,
}

impl Default for ComputePricingEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl ComputePricingEngine {
    pub fn new() -> Self {
        Self::from_str(BUNDLED_CATALOG)
    }

    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Self {
        let catalog: Value = match serde_json::from_str(s) {
            Ok(v) => v,
            Err(e) => {
                warn_once(
                    "catalog_malformed",
                    &format!("compute catalog malformed ({}); using HARDCODED", e),
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

    /// Look up a `_meta.default_*` rate. Falls back to HARDCODED on missing /
    /// malformed catalog.
    fn meta_default(&self, key: &str) -> Decimal {
        if let Some(s) = self
            .catalog
            .get("_meta")
            .and_then(|m| m.get(key))
            .and_then(|v| v.as_str())
        {
            if let Ok(d) = Decimal::from_str(s) {
                return d;
            }
        }
        *HARDCODED.get(key).unwrap_or(&Decimal::ZERO)
    }

    /// Public entry. Dispatches on `details["billing_model"]`.
    pub fn resolve_compute_cost(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
        window_s: Option<Decimal>,
    ) -> ComputeCost {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.dispatch(details, cloud_env, overrides, window_s)
        }));
        match result {
            Ok(cost) => cost,
            Err(_) => {
                warn_once(
                    "tier5_panic",
                    "compute cost dispatch panicked; falling back to zero",
                );
                ComputeCost::zero_unknown()
            }
        }
    }

    fn dispatch(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
        window_s: Option<Decimal>,
    ) -> ComputeCost {
        let model = details.get("billing_model").and_then(|v| v.as_str()).unwrap_or("");
        match model {
            "lambda" => self.price_lambda(details, cloud_env, overrides),
            "fargate" => self.price_fargate(details, cloud_env, overrides),
            "cloud_run_request" => self.price_cloud_run_request(details, cloud_env, overrides),
            "cloud_run_instance" => self.price_cloud_run_instance(details, cloud_env, overrides),
            "cloud_functions" => self.price_cloud_functions(details, cloud_env, overrides),
            "azure_functions" => self.price_azure_functions(details, cloud_env, overrides),
            "vercel_fluid" => self.price_vercel_fluid(details, cloud_env, overrides),
            // Sprint 1 Theme F / §2.3.1 (B5): canonical billing_model
            // discriminators have no `_share` suffix — Python and Go both
            // emit "ec2" / "gce" / "azure_vm" / "k8s_pod", and the cross-
            // SDK fixtures use the same. Pre-fix Rust matched `_share`
            // variants and silently fell through to zero_unknown for
            // every canonical payload.
            "ec2" => self.price_iaas_share(details, cloud_env, overrides, "aws", "ec2", window_s),
            "gce" => self.price_iaas_share(details, cloud_env, overrides, "gcp", "gce", window_s),
            "azure_vm" => self.price_iaas_share(details, cloud_env, overrides, "azure", "vm", window_s),
            "k8s_pod" => self.price_k8s_pod_share(details, overrides, window_s),
            _ => ComputeCost::zero_unknown(),
        }
    }

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

    fn dec_from(v: &Value) -> Option<Decimal> {
        v.as_str().and_then(|s| Decimal::from_str(s).ok())
    }

    fn override_rate(overrides: &HashMap<String, String>, key: &str) -> Option<Decimal> {
        overrides.get(key).and_then(|s| Decimal::from_str(s).ok())
    }

    // ── Lambda ─────────────────────────────────────────────────────────────
    fn price_lambda(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        let arch = details.get("architecture").and_then(|v| v.as_str()).unwrap_or("x86_64");
        let memory_mb = Self::dec_field(details, "lambda_memory_mb");
        let duration_ms = Self::dec_field(details, "duration_ms");
        let invocations = if details.get("invocation_count").is_some() {
            Self::dec_field(details, "invocation_count")
        } else {
            Decimal::ONE
        };
        let memory_gb = memory_mb / Decimal::from(1024u32); // Lambda billing GB
        let duration_s = duration_ms / *MS_PER_S;
        let gb_seconds = memory_gb * duration_s;

        // Resolve rates
        let region = cloud_env.region.as_deref();
        let mut source = String::from("compute_catalog:lambda:default");
        let mut request_usd = self.meta_default("default_lambda_request_usd");
        let mut gb_second_usd = self.meta_default("default_lambda_gb_second_usd");
        let mut confidence = "estimated";

        if let Some(block) = self.catalog.get("aws").and_then(|a| a.get("lambda")) {
            if let Some(r) = region {
                if let Some(regblock) = block.get("regions").and_then(|rs| rs.get(r)) {
                    if let Some(archblock) = regblock.get(arch) {
                        if let Some(req) = archblock.get("request_usd").and_then(Self::dec_from) {
                            request_usd = req;
                        }
                        if let Some(gbs) = archblock.get("gb_second_usd").and_then(Self::dec_from) {
                            gb_second_usd = gbs;
                        }
                        source = format!("compute_catalog:lambda:{}:{}", r, arch);
                        confidence = "computed";
                    }
                }
            }
            if confidence == "estimated" {
                if let Some(defblock) = block.get("default").and_then(|d| d.get(arch)) {
                    if let Some(req) = defblock.get("request_usd").and_then(Self::dec_from) {
                        request_usd = req;
                    }
                    if let Some(gbs) = defblock.get("gb_second_usd").and_then(Self::dec_from) {
                        gb_second_usd = gbs;
                    }
                    source = format!("compute_catalog:lambda:default:{}", arch);
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, "lambda_request_usd") {
            request_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "lambda_gb_second_usd") {
            gb_second_usd = o;
        }

        let cost = invocations * request_usd + gb_seconds * gb_second_usd;
        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── Fargate (BINARY GiB) ──────────────────────────────────────────────
    fn price_fargate(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        let arch = details.get("architecture").and_then(|v| v.as_str()).unwrap_or("x86_64");
        let vcpu = Self::dec_field(details, "fargate_vcpu");
        let memory_bytes = Self::dec_field(details, "fargate_memory_bytes_limit");
        let duration_ms = Self::dec_field(details, "duration_ms");
        let duration_s = duration_ms / *MS_PER_S;
        // BINARY GiB — Decision #6 / load-bearing test.
        let memory_gib = memory_bytes / *GIB_BINARY;

        let region = cloud_env.region.as_deref();
        let mut source = String::from("compute_catalog:fargate:default");
        let mut vcpu_second_usd = self.meta_default("default_fargate_vcpu_second_usd");
        let mut gib_second_usd = self.meta_default("default_fargate_gib_second_usd");
        let mut confidence = "estimated";

        if let Some(block) = self.catalog.get("aws").and_then(|a| a.get("fargate")) {
            if let Some(r) = region {
                if let Some(regblock) = block.get("regions").and_then(|rs| rs.get(r)) {
                    if let Some(archblock) = regblock.get(arch) {
                        if let Some(v) = archblock.get("vcpu_second_usd").and_then(Self::dec_from) {
                            vcpu_second_usd = v;
                        }
                        if let Some(g) = archblock.get("gib_second_usd").and_then(Self::dec_from) {
                            gib_second_usd = g;
                        }
                        source = format!("compute_catalog:fargate:{}:{}", r, arch);
                        confidence = "computed";
                    }
                }
            }
            if confidence == "estimated" {
                if let Some(defblock) = block.get("default").and_then(|d| d.get(arch)) {
                    if let Some(v) = defblock.get("vcpu_second_usd").and_then(Self::dec_from) {
                        vcpu_second_usd = v;
                    }
                    if let Some(g) = defblock.get("gib_second_usd").and_then(Self::dec_from) {
                        gib_second_usd = g;
                    }
                    source = format!("compute_catalog:fargate:default:{}", arch);
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, "fargate_vcpu_second_usd") {
            vcpu_second_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "fargate_gib_second_usd") {
            gib_second_usd = o;
        }

        let cost = vcpu * duration_s * vcpu_second_usd + memory_gib * duration_s * gib_second_usd;
        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── Cloud Run request (per-request) ───────────────────────────────────
    fn price_cloud_run_request(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        self.price_cloud_run_like(details, cloud_env, overrides, true, "cloud_run")
    }
    fn price_cloud_run_instance(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        // Instance-based billing doesn't have per-request charge.
        self.price_cloud_run_like(details, cloud_env, overrides, false, "cloud_run")
    }
    fn price_cloud_functions(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        // Functions Gen2 use the same rate keys as Cloud Run.
        self.price_cloud_run_like(details, cloud_env, overrides, true, "cloud_functions")
    }

    fn price_cloud_run_like(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
        include_request: bool,
        runtime_key: &str,
    ) -> ComputeCost {
        let vcpu = Self::dec_field(details, "vcpu_count");
        let memory_bytes = Self::dec_field(details, "memory_bytes");
        let duration_ms = Self::dec_field(details, "duration_ms");
        let invocations = if details.get("invocation_count").is_some() {
            Self::dec_field(details, "invocation_count")
        } else {
            Decimal::ONE
        };
        let duration_s = duration_ms / *MS_PER_S;
        let memory_gib = memory_bytes / *GIB_BINARY; // BINARY for Cloud Run

        let region = cloud_env.region.as_deref();
        let mut source = format!("compute_catalog:{}:default", runtime_key);
        let mut request_usd = self.meta_default("default_cloud_run_request_usd");
        let mut vcpu_second_usd = self.meta_default("default_cloud_run_vcpu_second_usd");
        let mut gib_second_usd = self.meta_default("default_cloud_run_gib_second_usd");
        let mut confidence = "estimated";

        if let Some(block) = self.catalog.get("gcp").and_then(|g| g.get(runtime_key)) {
            if let Some(r) = region {
                if let Some(regblock) = block.get("regions").and_then(|rs| rs.get(r)) {
                    if let Some(v) = regblock.get("request_usd").and_then(Self::dec_from) {
                        request_usd = v;
                    }
                    if let Some(v) = regblock.get("vcpu_second_usd").and_then(Self::dec_from) {
                        vcpu_second_usd = v;
                    }
                    if let Some(v) = regblock.get("gib_second_usd").and_then(Self::dec_from) {
                        gib_second_usd = v;
                    }
                    source = format!("compute_catalog:{}:{}", runtime_key, r);
                    confidence = "computed";
                }
            }
            if confidence == "estimated" {
                if let Some(defblock) = block.get("default") {
                    if let Some(v) = defblock.get("request_usd").and_then(Self::dec_from) {
                        request_usd = v;
                    }
                    if let Some(v) = defblock.get("vcpu_second_usd").and_then(Self::dec_from) {
                        vcpu_second_usd = v;
                    }
                    if let Some(v) = defblock.get("gib_second_usd").and_then(Self::dec_from) {
                        gib_second_usd = v;
                    }
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, "cloud_run_request_usd") {
            request_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "cloud_run_vcpu_second_usd") {
            vcpu_second_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "cloud_run_gib_second_usd") {
            gib_second_usd = o;
        }

        let mut cost = vcpu * duration_s * vcpu_second_usd + memory_gib * duration_s * gib_second_usd;
        if include_request {
            cost += invocations * request_usd;
        }
        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── Azure Functions (DECIMAL GB) ──────────────────────────────────────
    fn price_azure_functions(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        let memory_bytes = Self::dec_field(details, "memory_bytes");
        let duration_ms = Self::dec_field(details, "duration_ms");
        let invocations = if details.get("invocation_count").is_some() {
            Self::dec_field(details, "invocation_count")
        } else {
            Decimal::ONE
        };
        let duration_s = duration_ms / *MS_PER_S;
        let memory_gb = memory_bytes / *GB_DECIMAL; // DECIMAL for Azure Functions

        let region = cloud_env.region.as_deref();
        let mut source = String::from("compute_catalog:azure_functions:default");
        let mut execution_usd = self.meta_default("default_azure_functions_execution_usd");
        let mut gb_second_usd = self.meta_default("default_azure_functions_gb_second_usd");
        let mut confidence = "estimated";

        if let Some(block) = self.catalog.get("azure").and_then(|a| a.get("functions_consumption")) {
            if let Some(r) = region {
                if let Some(regblock) = block.get("regions").and_then(|rs| rs.get(r)) {
                    if let Some(v) = regblock.get("execution_usd").and_then(Self::dec_from) {
                        execution_usd = v;
                    }
                    if let Some(v) = regblock.get("gb_second_usd").and_then(Self::dec_from) {
                        gb_second_usd = v;
                    }
                    source = format!("compute_catalog:azure_functions:{}", r);
                    confidence = "computed";
                }
            }
            if confidence == "estimated" {
                if let Some(defblock) = block.get("default") {
                    if let Some(v) = defblock.get("execution_usd").and_then(Self::dec_from) {
                        execution_usd = v;
                    }
                    if let Some(v) = defblock.get("gb_second_usd").and_then(Self::dec_from) {
                        gb_second_usd = v;
                    }
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, "azure_functions_execution_usd") {
            execution_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "azure_functions_gb_second_usd") {
            gb_second_usd = o;
        }

        let cost = invocations * execution_usd + memory_gb * duration_s * gb_second_usd;
        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── Vercel Fluid (CPU-hour + memory-GB-hour) ──────────────────────────
    fn price_vercel_fluid(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
    ) -> ComputeCost {
        let vcpu = Self::dec_field(details, "vcpu_count");
        let memory_bytes = Self::dec_field(details, "memory_bytes");
        let duration_ms = Self::dec_field(details, "duration_ms");
        let memory_gb = memory_bytes / *GB_DECIMAL;
        let duration_hr = duration_ms / *MS_PER_S / *HOUR_S;

        let region = cloud_env.region.as_deref();
        let mut source = String::from("compute_catalog:vercel_fluid:default");
        let mut cpu_hour_usd = self.meta_default("default_vercel_cpu_hour_usd");
        let mut memory_gb_hour_usd = self.meta_default("default_vercel_memory_gb_hour_usd");
        let mut confidence = "estimated";

        if let Some(block) = self.catalog.get("vercel").and_then(|v| v.get("fluid")) {
            if let Some(r) = region {
                if let Some(regblock) = block.get("regions").and_then(|rs| rs.get(r)) {
                    if let Some(v) = regblock.get("cpu_hour_usd").and_then(Self::dec_from) {
                        cpu_hour_usd = v;
                    }
                    if let Some(v) = regblock.get("memory_gb_hour_usd").and_then(Self::dec_from) {
                        memory_gb_hour_usd = v;
                    }
                    source = format!("compute_catalog:vercel_fluid:{}", r);
                    confidence = "computed";
                }
            }
            if confidence == "estimated" {
                if let Some(defblock) = block.get("default") {
                    if let Some(v) = defblock.get("cpu_hour_usd").and_then(Self::dec_from) {
                        cpu_hour_usd = v;
                    }
                    if let Some(v) = defblock.get("memory_gb_hour_usd").and_then(Self::dec_from) {
                        memory_gb_hour_usd = v;
                    }
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, "vercel_cpu_hour_usd") {
            cpu_hour_usd = o;
        }
        if let Some(o) = Self::override_rate(overrides, "vercel_memory_gb_hour_usd") {
            memory_gb_hour_usd = o;
        }

        let cost = vcpu * duration_hr * cpu_hour_usd + memory_gb * duration_hr * memory_gb_hour_usd;
        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── EC2 / GCE / AzureVm share — SKU hourly_usd × duration ─────────────
    fn price_iaas_share(
        &self,
        details: &Value,
        cloud_env: &CloudEnv,
        overrides: &HashMap<String, String>,
        provider: &str,
        runtime: &str,
        _window_s: Option<Decimal>,
    ) -> ComputeCost {
        let duration_ms = Self::dec_field(details, "duration_ms");
        let duration_s = duration_ms / *MS_PER_S;
        let duration_hr = duration_s / *HOUR_S;

        // Per Decision #9 — idle is INVISIBLE on long-running runtimes.
        // The accountant only emits when actual CPU was consumed; the share
        // path bills duration × (vcpu_used/vcpu_total) × hourly. We don't
        // synthesize idle time here; the caller must pass vcpu_seconds_used.
        let vcpu_seconds_used = Self::dec_field(details, "vcpu_seconds_used");

        let region = cloud_env.region.as_deref();
        let instance_type = cloud_env.instance_type.as_deref();
        let mut source = format!("compute_catalog:{}:default", runtime);
        let mut hourly_usd: Option<Decimal> = None;
        let mut vcpu_count: Option<Decimal> = None;
        let mut confidence = "estimated";

        if let (Some(r), Some(it)) = (region, instance_type) {
            if let Some(block) = self
                .catalog
                .get(provider)
                .and_then(|p| p.get(runtime))
                .and_then(|rt| rt.get("regions"))
                .and_then(|rs| rs.get(r))
                .and_then(|reg| reg.get("instance_types"))
                .and_then(|its| its.get(it))
            {
                if let Some(v) = block.get("hourly_usd").and_then(Self::dec_from) {
                    hourly_usd = Some(v);
                }
                if let Some(v) = block.get("vcpu_count").and_then(Self::dec_from) {
                    vcpu_count = Some(v);
                }
                if hourly_usd.is_some() && vcpu_count.is_some() {
                    source = format!("compute_catalog:{}:{}:{}", runtime, r, it);
                    confidence = "computed";
                }
            }
        }

        if let Some(o) = Self::override_rate(overrides, &format!("{}_vcpu_hour_usd", runtime)) {
            // Direct vCPU-hour override: cost = vcpu_seconds_used / 3600 * rate
            let cost = vcpu_seconds_used / *HOUR_S * o;
            return ComputeCost {
                cost_usd: cost,
                pricing_source: format!("compute_catalog:{}:override", runtime),
                cost_confidence: "estimated".to_string(),
            };
        }

        let cost = match (hourly_usd, vcpu_count) {
            (Some(h), Some(v)) if v > Decimal::ZERO => {
                // share = vcpu_seconds_used / (vcpu_count * 3600)
                let share = vcpu_seconds_used / (v * *HOUR_S);
                share * h * duration_hr / (duration_hr.max(Decimal::ONE))
                    // Simplify: vcpu_seconds_used / (vcpu_count * 3600) * hourly_usd
            }
            _ => {
                // Tier 4: use per-vcpu-hour default
                let per_vcpu = self.meta_default("default_ec2_vcpu_hour_usd");
                vcpu_seconds_used / *HOUR_S * per_vcpu
            }
        };

        ComputeCost {
            cost_usd: cost,
            pricing_source: source,
            cost_confidence: confidence.to_string(),
        }
    }

    // ── k8s pod share — vcpu-hour × duration ──────────────────────────────
    fn price_k8s_pod_share(
        &self,
        details: &Value,
        overrides: &HashMap<String, String>,
        _window_s: Option<Decimal>,
    ) -> ComputeCost {
        let vcpu_seconds_used = Self::dec_field(details, "vcpu_seconds_used");
        let per_vcpu = if let Some(o) = Self::override_rate(overrides, "k8s_pod_vcpu_hour_usd") {
            o
        } else {
            self.meta_default("default_k8s_pod_vcpu_hour_usd")
        };
        let cost = vcpu_seconds_used / *HOUR_S * per_vcpu;
        ComputeCost {
            cost_usd: cost,
            pricing_source: "compute_catalog:k8s_pod:default".to_string(),
            cost_confidence: "estimated".to_string(),
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;
    use serde_json::json;

    fn cloud_env_aws(region: &str) -> CloudEnv {
        CloudEnv {
            provider: Some("aws".into()),
            region: Some(region.into()),
            source: "imds",
            instance_type: None,
        }
    }

    // ============================================================ Catalog integrity (Task 5)

    fn catalog() -> Value {
        serde_json::from_str(BUNDLED_CATALOG).expect("catalog parses")
    }

    #[test]
    fn catalog_parses_as_json() {
        let data = catalog();
        assert!(data.get("_meta").is_some());
    }

    #[test]
    fn meta_has_required_default_keys() {
        let data = catalog();
        let meta = data.get("_meta").unwrap();
        for k in [
            "version",
            "last_updated",
            "currency",
            "default_lambda_request_usd",
            "default_lambda_gb_second_usd",
            "default_fargate_vcpu_second_usd",
            "default_fargate_gib_second_usd",
            "default_cloud_run_request_usd",
            "default_cloud_run_vcpu_second_usd",
            "default_cloud_run_gib_second_usd",
            "default_azure_functions_execution_usd",
            "default_azure_functions_gb_second_usd",
            "default_vercel_cpu_hour_usd",
            "default_vercel_memory_gb_hour_usd",
            "default_ec2_vcpu_hour_usd",
            "default_k8s_pod_vcpu_hour_usd",
            "description",
            "notes",
        ] {
            assert!(meta.get(k).is_some(), "_meta missing {}", k);
            if k.starts_with("default_") && k.ends_with("_usd") {
                let s = meta.get(k).unwrap().as_str().unwrap();
                Decimal::from_str(s).expect(k);
            }
        }
        assert_eq!(meta.get("currency").unwrap().as_str(), Some("USD"));
    }

    #[test]
    fn every_provider_has_last_verified_freshness_soft_warn() {
        let data = catalog();
        for (provider, block) in data.as_object().unwrap() {
            if provider == "_meta" {
                continue;
            }
            let verified = block.get("_last_verified").and_then(|v| v.as_str()).unwrap();
            // Soft-warn only — date parse uses simple lexical comparison.
            if verified < "2025-11-22" {
                eprintln!(
                    "[soft-warn] compute_prices.json: {} _last_verified is stale: {}",
                    provider, verified
                );
            }
        }
    }

    #[test]
    fn all_providers_and_runtimes_present() {
        let data = catalog();
        for p in ["aws", "gcp", "azure", "vercel"] {
            assert!(data.get(p).is_some(), "{} missing", p);
        }
        let aws = data.get("aws").unwrap();
        for r in ["lambda", "fargate", "ec2"] {
            assert!(aws.get(r).is_some(), "aws.{} missing", r);
        }
        let gcp = data.get("gcp").unwrap();
        for r in ["cloud_run", "cloud_functions", "gce"] {
            assert!(gcp.get(r).is_some(), "gcp.{} missing", r);
        }
        let azure = data.get("azure").unwrap();
        for r in ["functions_consumption", "vm"] {
            assert!(azure.get(r).is_some(), "azure.{} missing", r);
        }
        assert!(data.get("vercel").unwrap().get("fluid").is_some());
    }

    #[test]
    fn lambda_has_both_architectures() {
        let data = catalog();
        let default = data["aws"]["lambda"]["default"].as_object().unwrap();
        for k in default.keys() {
            assert!(k == "x86_64" || k == "arm64", "lambda default has {}", k);
        }
        for arch in ["x86_64", "arm64"] {
            let s = data["aws"]["lambda"]["default"][arch]["request_usd"]
                .as_str()
                .unwrap();
            Decimal::from_str(s).unwrap();
        }
    }

    #[test]
    fn fargate_has_both_architectures() {
        let data = catalog();
        for arch in ["x86_64", "arm64"] {
            let s = data["aws"]["fargate"]["default"][arch]["vcpu_second_usd"]
                .as_str()
                .unwrap();
            Decimal::from_str(s).unwrap();
        }
    }

    #[test]
    fn arm_cheaper_than_x86_on_lambda() {
        let data = catalog();
        let regions = data["aws"]["lambda"]["regions"].as_object().unwrap();
        let (_, first) = regions.iter().next().unwrap();
        let arm =
            Decimal::from_str(first["arm64"]["gb_second_usd"].as_str().unwrap()).unwrap();
        let x86 =
            Decimal::from_str(first["x86_64"]["gb_second_usd"].as_str().unwrap()).unwrap();
        assert!(arm < x86, "arm64 must be cheaper than x86_64 on Lambda");
    }

    #[test]
    fn arm_cheaper_than_x86_on_fargate() {
        let data = catalog();
        let regions = data["aws"]["fargate"]["regions"].as_object().unwrap();
        let (_, first) = regions.iter().next().unwrap();
        let arm =
            Decimal::from_str(first["arm64"]["vcpu_second_usd"].as_str().unwrap()).unwrap();
        let x86 =
            Decimal::from_str(first["x86_64"]["vcpu_second_usd"].as_str().unwrap()).unwrap();
        assert!(arm < x86, "arm64 must be cheaper than x86_64 on Fargate");
    }

    #[test]
    fn top_instance_types_present_for_ec2_us_east_1() {
        let data = catalog();
        let its = &data["aws"]["ec2"]["regions"]["us-east-1"]["instance_types"];
        for must in ["c7g.xlarge", "m7i.large", "t3.medium"] {
            assert!(its.get(must).is_some(), "missing EC2 SKU: {}", must);
        }
    }

    #[test]
    fn top_instance_types_present_for_gce_us_central1() {
        let data = catalog();
        let its = &data["gcp"]["gce"]["regions"]["us-central1"]["instance_types"];
        for must in ["n2-standard-2", "e2-standard-4"] {
            assert!(its.get(must).is_some(), "missing GCE SKU: {}", must);
        }
    }

    #[test]
    fn top_instance_types_present_for_azure_vm_eastus() {
        let data = catalog();
        let its = &data["azure"]["vm"]["regions"]["eastus"]["instance_types"];
        for must in ["Standard_D2s_v3", "Standard_B2ms"] {
            assert!(its.get(must).is_some(), "missing Azure VM SKU: {}", must);
        }
    }

    #[test]
    fn every_rate_is_decimal_parseable() {
        let data = catalog();
        fn walk(node: &Value, path: &str) {
            match node {
                Value::Object(map) => {
                    for (k, v) in map {
                        walk(v, &format!("{}.{}", path, k));
                    }
                }
                Value::String(s) => {
                    if path.ends_with("_usd") || path.ends_with("vcpu_count") {
                        Decimal::from_str(s).unwrap_or_else(|_| {
                            panic!("{} not Decimal-parseable: {:?}", path, s)
                        });
                    }
                }
                _ => {}
            }
        }
        walk(&data, "");
    }

    #[test]
    fn every_dispatch_billing_model_has_a_rate_path() {
        let data = catalog();
        let meta = data.get("_meta").unwrap();
        for k in [
            "default_lambda_request_usd",
            "default_fargate_vcpu_second_usd",
            "default_cloud_run_request_usd",
            "default_azure_functions_execution_usd",
            "default_vercel_cpu_hour_usd",
            "default_ec2_vcpu_hour_usd",
            "default_k8s_pod_vcpu_hour_usd",
        ] {
            assert!(meta.get(k).is_some(), "{}", k);
        }
    }

    // ============================================================ Engine (Task 6)

    #[test]
    fn lambda_us_east_1_arm64_yields_computed() {
        let eng = ComputePricingEngine::new();
        let details = json!({
            "billing_model": "lambda",
            "architecture": "arm64",
            "lambda_memory_mb": "512",
            "duration_ms": "1000",
        });
        let c = eng.resolve_compute_cost(
            &details,
            &cloud_env_aws("us-east-1"),
            &HashMap::new(),
            None,
        );
        assert!(c.cost_usd > Decimal::ZERO);
        assert_eq!(c.cost_confidence, "computed");
        assert!(c.pricing_source.starts_with("compute_catalog:lambda:us-east-1:arm64"));
    }

    #[test]
    fn lambda_unknown_region_falls_to_default_arch_estimated() {
        let eng = ComputePricingEngine::new();
        let details = json!({
            "billing_model": "lambda",
            "architecture": "x86_64",
            "lambda_memory_mb": "256",
            "duration_ms": "500",
        });
        let c = eng.resolve_compute_cost(
            &details,
            &cloud_env_aws("moon-base-1"),
            &HashMap::new(),
            None,
        );
        assert_eq!(c.cost_confidence, "estimated");
        assert!(c.pricing_source.contains("default"));
    }

    #[test]
    fn fargate_uses_binary_gib_divisor() {
        // CRITICAL: Decision #6 + load-bearing test. Using decimal GB
        // (10^9) instead of binary GiB (2^30) causes ~4.86% silent
        // over-attribution.
        let eng = ComputePricingEngine::new();
        // 1 GiB = 1_073_741_824 bytes, duration 1s, vcpu 1, gib_second_usd from catalog
        let details = json!({
            "billing_model": "fargate",
            "architecture": "x86_64",
            "fargate_vcpu": "1",
            "fargate_memory_bytes_limit": "1073741824",
            "duration_ms": "1000",
        });
        let c = eng.resolve_compute_cost(
            &details,
            &cloud_env_aws("us-east-1"),
            &HashMap::new(),
            None,
        );
        // Expected: 1 * 1 * vcpu_second + (1073741824 / 1073741824) * 1 * gib_second
        // The memory contribution should be exactly gib_second_usd (1.0 GiB).
        // If decimal divisor used by mistake, contribution would be ~1.0737 GB → off by 7.37%.
        let vcpu_rate = dec!(0.0000112444);
        let gib_rate = dec!(0.0000012347);
        let expected = vcpu_rate + gib_rate;
        let diff = (c.cost_usd - expected).abs();
        assert!(
            diff < dec!(0.00000001),
            "binary GiB divisor required (got {}, expected {})",
            c.cost_usd,
            expected
        );
    }

    #[test]
    fn fargate_arm64_cheaper_than_x86() {
        let eng = ComputePricingEngine::new();
        let base = |arch: &str| {
            json!({
                "billing_model": "fargate",
                "architecture": arch,
                "fargate_vcpu": "1",
                "fargate_memory_bytes_limit": "2147483648",
                "duration_ms": "60000",
            })
        };
        let arm = eng.resolve_compute_cost(&base("arm64"), &cloud_env_aws("us-east-1"), &HashMap::new(), None);
        let x86 = eng.resolve_compute_cost(&base("x86_64"), &cloud_env_aws("us-east-1"), &HashMap::new(), None);
        assert!(arm.cost_usd < x86.cost_usd, "arm64 must be cheaper");
    }

    #[test]
    fn lambda_arm64_cheaper_than_x86() {
        let eng = ComputePricingEngine::new();
        let base = |arch: &str| {
            json!({
                "billing_model": "lambda",
                "architecture": arch,
                "lambda_memory_mb": "1024",
                "duration_ms": "1000",
            })
        };
        let arm = eng.resolve_compute_cost(&base("arm64"), &cloud_env_aws("us-east-1"), &HashMap::new(), None);
        let x86 = eng.resolve_compute_cost(&base("x86_64"), &cloud_env_aws("us-east-1"), &HashMap::new(), None);
        assert!(arm.cost_usd < x86.cost_usd, "arm64 must be cheaper");
    }

    #[test]
    fn cloud_run_request_includes_request_charge() {
        let eng = ComputePricingEngine::new();
        let details = json!({
            "billing_model": "cloud_run_request",
            "vcpu_count": "1",
            "memory_bytes": "536870912",
            "duration_ms": "500",
            "invocation_count": "1",
        });
        let env = CloudEnv {
            provider: Some("gcp".into()),
            region: Some("us-central1".into()),
            source: "imds",
            instance_type: None,
        };
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd > Decimal::ZERO);
    }

    #[test]
    fn cloud_run_instance_excludes_request_charge() {
        let eng = ComputePricingEngine::new();
        let env = CloudEnv {
            provider: Some("gcp".into()),
            region: Some("us-central1".into()),
            source: "imds",
            instance_type: None,
        };
        let request_d = json!({
            "billing_model": "cloud_run_request",
            "vcpu_count": "1",
            "memory_bytes": "536870912",
            "duration_ms": "500",
            "invocation_count": "1",
        });
        let instance_d = json!({
            "billing_model": "cloud_run_instance",
            "vcpu_count": "1",
            "memory_bytes": "536870912",
            "duration_ms": "500",
        });
        let r = eng.resolve_compute_cost(&request_d, &env, &HashMap::new(), None);
        let i = eng.resolve_compute_cost(&instance_d, &env, &HashMap::new(), None);
        assert!(r.cost_usd > i.cost_usd, "request has extra per-invoke charge");
    }

    #[test]
    fn azure_functions_uses_decimal_gb() {
        let eng = ComputePricingEngine::new();
        let details = json!({
            "billing_model": "azure_functions",
            "memory_bytes": "1000000000",
            "duration_ms": "1000",
            "invocation_count": "1",
        });
        let env = CloudEnv {
            provider: Some("azure".into()),
            region: Some("eastus".into()),
            source: "imds",
            instance_type: None,
        };
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd > Decimal::ZERO);
    }

    #[test]
    fn vercel_fluid_yields_positive_cost() {
        let eng = ComputePricingEngine::new();
        let details = json!({
            "billing_model": "vercel_fluid",
            "vcpu_count": "0.5",
            "memory_bytes": "1073741824",
            "duration_ms": "3600000", // 1 hour
        });
        let env = CloudEnv {
            provider: Some("vercel".into()),
            region: Some("iad1".into()),
            source: "env",
            instance_type: None,
        };
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd > Decimal::ZERO);
    }

    #[test]
    fn ec2_share_with_sku_yields_computed() {
        let eng = ComputePricingEngine::new();
        let env = CloudEnv {
            provider: Some("aws".into()),
            region: Some("us-east-1".into()),
            source: "imds",
            instance_type: Some("c7g.xlarge".into()),
        };
        let details = json!({
            "billing_model": "ec2",
            "duration_ms": "60000",
            "vcpu_seconds_used": "30", // 30 vcpu-seconds
        });
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd > Decimal::ZERO);
        assert_eq!(c.cost_confidence, "computed");
    }

    #[test]
    fn k8s_pod_share_yields_positive_cost() {
        let eng = ComputePricingEngine::new();
        let env = CloudEnv {
            provider: None,
            region: None,
            source: "none",
            instance_type: None,
        };
        let details = json!({
            "billing_model": "k8s_pod",
            "duration_ms": "60000",
            "vcpu_seconds_used": "30",
        });
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd > Decimal::ZERO);
    }

    #[test]
    fn unknown_billing_model_returns_zero() {
        let eng = ComputePricingEngine::new();
        let env = CloudEnv::none();
        let details = json!({"billing_model": "totally_unknown"});
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert_eq!(c.cost_usd, Decimal::ZERO);
    }

    #[test]
    fn pricing_source_starts_with_compute_catalog() {
        let eng = ComputePricingEngine::new();
        let env = cloud_env_aws("us-east-1");
        let details = json!({
            "billing_model": "lambda",
            "architecture": "x86_64",
            "lambda_memory_mb": "128",
            "duration_ms": "100",
        });
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.pricing_source.starts_with("compute_catalog:"));
    }

    #[test]
    fn malformed_catalog_falls_through_to_hardcoded() {
        let eng = ComputePricingEngine::from_str("{not json");
        // Lambda dispatch should still return a non-panicking result using
        // HARDCODED defaults.
        let env = CloudEnv::none();
        let details = json!({
            "billing_model": "lambda",
            "architecture": "x86_64",
            "lambda_memory_mb": "128",
            "duration_ms": "100",
        });
        let c = eng.resolve_compute_cost(&details, &env, &HashMap::new(), None);
        assert!(c.cost_usd >= Decimal::ZERO);
        assert_eq!(c.cost_confidence, "estimated");
    }

    #[test]
    fn override_lambda_request_usd_wins_over_catalog() {
        let eng = ComputePricingEngine::new();
        let env = cloud_env_aws("us-east-1");
        let details = json!({
            "billing_model": "lambda",
            "architecture": "x86_64",
            "lambda_memory_mb": "128",
            "duration_ms": "0",
            "invocation_count": "1",
        });
        let mut o = HashMap::new();
        o.insert("lambda_request_usd".to_string(), "99.99".to_string());
        let c = eng.resolve_compute_cost(&details, &env, &o, None);
        assert!(c.cost_usd > Decimal::from(99u32));
    }

    #[test]
    fn catalog_version_from_meta() {
        let eng = ComputePricingEngine::new();
        let v = eng.catalog_version();
        assert!(!v.is_empty() && v != "unknown");
    }
}
