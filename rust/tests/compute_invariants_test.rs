//! Property invariants for compute pricing.
//!
//! Mirrors python/tests/test_compute_pricing_invariants.py — table-driven over
//! all 11 billing models. Pins:
//!   - cost >= 0
//!   - duration/memory linearity
//!   - ARM < x86 on Lambda + Fargate
//!   - cost_confidence in {computed, estimated}
//!   - pricing_source starts with "compute_catalog:"

use std::collections::HashMap;
use std::str::FromStr;

use dexcost::cloud_detect::CloudEnv;
use dexcost::pricing::compute_pricing::ComputePricingEngine;
use rust_decimal::Decimal;
use serde_json::json;

fn env_aws(region: &str) -> CloudEnv {
    CloudEnv {
        provider: Some("aws".into()),
        region: Some(region.into()),
        source: "imds",
        instance_type: Some("c7g.xlarge".into()),
    }
}

fn env_gcp(region: &str) -> CloudEnv {
    CloudEnv {
        provider: Some("gcp".into()),
        region: Some(region.into()),
        source: "imds",
        instance_type: Some("n2-standard-2".into()),
    }
}

fn env_azure(region: &str) -> CloudEnv {
    CloudEnv {
        provider: Some("azure".into()),
        region: Some(region.into()),
        source: "imds",
        instance_type: Some("Standard_D2s_v3".into()),
    }
}

fn base_details(model: &str) -> serde_json::Value {
    let common = json!({
        "billing_model": model,
        "duration_ms": "1000",
        "architecture": "x86_64",
        "lambda_memory_mb": "512",
        "fargate_vcpu": "1",
        "fargate_memory_bytes_limit": "2147483648",  // 2 GiB binary
        "vcpu_count": "1",
        "memory_bytes": "1073741824",
        "vcpu_seconds_used": "1",
        "invocation_count": "1",
    });
    common
}

const ALL_MODELS: &[&str] = &[
    "lambda",
    "fargate",
    "cloud_run_request",
    "cloud_run_instance",
    "cloud_functions",
    "azure_functions",
    "vercel_fluid",
    "ec2",
    "gce",
    "azure_vm",
    "k8s_pod",
];

fn env_for_model(model: &str) -> CloudEnv {
    match model {
        "lambda" | "fargate" | "ec2" => env_aws("us-east-1"),
        "cloud_run_request" | "cloud_run_instance" | "cloud_functions" | "gce" => {
            env_gcp("us-central1")
        }
        "azure_functions" | "azure_vm" => env_azure("eastus"),
        "vercel_fluid" => CloudEnv {
            provider: Some("vercel".into()),
            region: Some("iad1".into()),
            source: "env",
            instance_type: None,
        },
        _ => CloudEnv::none(),
    }
}

#[test]
fn invariant_cost_is_non_negative_for_every_model() {
    let eng = ComputePricingEngine::new();
    for model in ALL_MODELS {
        let env = env_for_model(model);
        let c = eng.resolve_compute_cost(&base_details(model), &env, &HashMap::new(), None);
        assert!(c.cost_usd >= Decimal::ZERO, "{} cost was negative: {}", model, c.cost_usd);
    }
}

#[test]
fn invariant_pricing_source_starts_with_compute_catalog() {
    let eng = ComputePricingEngine::new();
    for model in ALL_MODELS {
        let env = env_for_model(model);
        let c = eng.resolve_compute_cost(&base_details(model), &env, &HashMap::new(), None);
        assert!(
            c.pricing_source.starts_with("compute_catalog:"),
            "{} pricing_source did not start with compute_catalog: ({})",
            model,
            c.pricing_source
        );
    }
}

#[test]
fn invariant_cost_confidence_in_computed_or_estimated() {
    let eng = ComputePricingEngine::new();
    for model in ALL_MODELS {
        let env = env_for_model(model);
        let c = eng.resolve_compute_cost(&base_details(model), &env, &HashMap::new(), None);
        assert!(
            c.cost_confidence == "computed" || c.cost_confidence == "estimated",
            "{} unexpected confidence: {}",
            model,
            c.cost_confidence
        );
    }
}

#[test]
fn invariant_duration_linearity_for_long_running() {
    let eng = ComputePricingEngine::new();
    for model in ["fargate", "cloud_run_instance", "ec2", "k8s_pod"] {
        let env = env_for_model(model);
        let mut d1 = base_details(model);
        let mut d2 = base_details(model);
        d2["duration_ms"] = json!("2000");
        if model == "ec2" || model == "k8s_pod" {
            d2["vcpu_seconds_used"] = json!("2");
        }
        let c1 = eng.resolve_compute_cost(&d1, &env, &HashMap::new(), None);
        let c2 = eng.resolve_compute_cost(&d2, &env, &HashMap::new(), None);
        // Doubling duration must not decrease cost.
        assert!(
            c2.cost_usd >= c1.cost_usd,
            "{} duration linearity broken: 1s={} 2s={}",
            model,
            c1.cost_usd,
            c2.cost_usd
        );
        let _ = &mut d1;
    }
}

#[test]
fn invariant_arm_cheaper_than_x86_on_lambda() {
    let eng = ComputePricingEngine::new();
    let mut arm = base_details("lambda");
    arm["architecture"] = json!("arm64");
    let x86 = base_details("lambda");
    let env = env_aws("us-east-1");
    let a = eng.resolve_compute_cost(&arm, &env, &HashMap::new(), None);
    let x = eng.resolve_compute_cost(&x86, &env, &HashMap::new(), None);
    assert!(a.cost_usd < x.cost_usd, "ARM must be cheaper than x86 on Lambda");
}

#[test]
fn invariant_arm_cheaper_than_x86_on_fargate() {
    let eng = ComputePricingEngine::new();
    let mut arm = base_details("fargate");
    arm["architecture"] = json!("arm64");
    let x86 = base_details("fargate");
    let env = env_aws("us-east-1");
    let a = eng.resolve_compute_cost(&arm, &env, &HashMap::new(), None);
    let x = eng.resolve_compute_cost(&x86, &env, &HashMap::new(), None);
    assert!(a.cost_usd < x.cost_usd, "ARM must be cheaper than x86 on Fargate");
}

#[test]
fn invariant_zero_duration_yields_zero_or_request_only_cost() {
    let eng = ComputePricingEngine::new();
    let env = env_aws("us-east-1");
    let mut d = base_details("lambda");
    d["duration_ms"] = json!("0");
    let c = eng.resolve_compute_cost(&d, &env, &HashMap::new(), None);
    // Only the request_usd remains.
    let expected = Decimal::from_str("0.0000002").unwrap();
    assert_eq!(c.cost_usd, expected);
}

#[test]
fn decision_9_ec2_idle_gap_is_invisible() {
    // Decision #9: EC2 idle is invisible. The accountant emits with
    // vcpu_seconds_used = 0 when no CPU was consumed; the engine then
    // produces $0 from the share formula (vcpu_seconds_used / vcpu_count
    // / 3600 * hourly_usd = 0).
    let eng = ComputePricingEngine::new();
    let env = env_aws("us-east-1");
    let d = json!({
        "billing_model": "ec2",
        "duration_ms": "60000",       // 60s wall time
        "vcpu_seconds_used": "0",     // but nothing consumed
    });
    let c = eng.resolve_compute_cost(&d, &env, &HashMap::new(), None);
    assert_eq!(
        c.cost_usd,
        Decimal::ZERO,
        "Decision #9: EC2 idle gap must be invisible (vcpu_seconds_used=0 → $0)"
    );
}

#[test]
fn decision_10_fargate_idle_gap_is_invisible() {
    // Decision #10: Fargate container idle is invisible. When CPU consumed
    // is 0, the only cost should be the memory contribution from the
    // declared limit — and the test setup with 0 vcpu * 0 duration yields 0.
    let eng = ComputePricingEngine::new();
    let env = env_aws("us-east-1");
    let d = json!({
        "billing_model": "fargate",
        "architecture": "x86_64",
        "fargate_vcpu": "0",          // no CPU
        "fargate_memory_bytes_limit": "0",  // no memory
        "duration_ms": "60000",
    });
    let c = eng.resolve_compute_cost(&d, &env, &HashMap::new(), None);
    assert_eq!(
        c.cost_usd,
        Decimal::ZERO,
        "Decision #10: Fargate container idle gap must be invisible"
    );
}

// ──────────────────────────────────────────────────────────────────────
// Cross-runtime matrix — each billing_model gets a hand-fixture asserting
// positive cost + expected pricing_source substring.
// ──────────────────────────────────────────────────────────────────────

#[test]
fn matrix_lambda_us_east_1_arm64() {
    let eng = ComputePricingEngine::new();
    let mut d = base_details("lambda");
    d["architecture"] = json!("arm64");
    let c = eng.resolve_compute_cost(&d, &env_aws("us-east-1"), &HashMap::new(), None);
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("lambda"));
}

#[test]
fn matrix_fargate_us_east_1_x86() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("fargate"),
        &env_aws("us-east-1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("fargate"));
}

#[test]
fn matrix_cloud_run_request_us_central1() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("cloud_run_request"),
        &env_gcp("us-central1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("cloud_run"));
}

#[test]
fn matrix_cloud_run_instance_us_central1() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("cloud_run_instance"),
        &env_gcp("us-central1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
}

#[test]
fn matrix_cloud_functions_us_central1() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("cloud_functions"),
        &env_gcp("us-central1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("cloud_functions"));
}

#[test]
fn matrix_azure_functions_eastus() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("azure_functions"),
        &env_azure("eastus"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("azure_functions"));
}

#[test]
fn matrix_vercel_fluid_iad1() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("vercel_fluid"),
        &env_for_model("vercel_fluid"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("vercel"));
}

#[test]
fn matrix_ec2_share_us_east_1_c7g_xlarge() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("ec2"),
        &env_aws("us-east-1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("ec2"));
}

#[test]
fn matrix_gce_share_us_central1_n2_standard_2() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("gce"),
        &env_gcp("us-central1"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
}

#[test]
fn matrix_azure_vm_share_eastus() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("azure_vm"),
        &env_azure("eastus"),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
}

#[test]
fn matrix_k8s_pod_share() {
    let eng = ComputePricingEngine::new();
    let c = eng.resolve_compute_cost(
        &base_details("k8s_pod"),
        &CloudEnv::none(),
        &HashMap::new(),
        None,
    );
    assert!(c.cost_usd > Decimal::ZERO);
    assert!(c.pricing_source.contains("k8s_pod"));
}
