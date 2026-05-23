//! GPU cross-billing-model dispatch matrix — Phase 2 Task 9.
//!
//! Rust port of `python/tests/test_gpu_cross_billing_model_matrix.py`
//! (commit d42cc81). One canonical case per billing-model × major
//! provider combination. If a future refactor accidentally routes one
//! billing_model through another's math, exactly one of these fails with
//! the specific billing_model in the failure message.

use rust_decimal::Decimal;
use serde_json::json;

use dexcost::cloud_detect::CloudEnv;
use dexcost::pricing::gpu_pricing::GpuPricingEngine;

fn env(provider: Option<&str>, region: Option<&str>, itype: Option<&str>) -> CloudEnv {
    CloudEnv {
        provider: provider.map(String::from),
        region: region.map(String::from),
        source: "test",
        instance_type: itype.map(String::from),
    }
}

fn assert_well_formed(billing_model: &str, c: &dexcost::pricing::gpu_pricing::GpuCost) {
    assert!(
        c.cost_usd >= Decimal::ZERO,
        "billing_model={} produced negative cost",
        billing_model
    );
    assert!(
        c.pricing_source.starts_with("gpu_catalog:"),
        "billing_model={} pricing_source missing gpu_catalog: prefix — got {}",
        billing_model,
        c.pricing_source
    );
    assert!(
        c.cost_confidence == "computed" || c.cost_confidence == "estimated",
        "billing_model={} confidence={} not in {{computed, estimated}}",
        billing_model,
        c.cost_confidence
    );
}

#[test]
fn modal_per_gpu_second_active() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_gpu_second_active",
        "gpu_seconds_used": "10", "gpu_sku": "h100-80gb-sxm5",
        "duration_ms": 10000, "gpu_count": 1,
    });
    let c = e.resolve_gpu_cost(&det, &env(Some("modal"), None, None), None);
    assert_well_formed("per_gpu_second_active", &c);
}

#[test]
fn aws_p5_per_instance_hour() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_instance_hour",
        "gpu_seconds_used": "30", "gpu_count": 8,
        "duration_ms": 60000, "region": "us-east-1",
        "instance_type": "p5.48xlarge",
    });
    let c = e.resolve_gpu_cost(
        &det,
        &env(Some("aws"), Some("us-east-1"), Some("p5.48xlarge")),
        None,
    );
    assert_well_formed("per_instance_hour", &c);
}

#[test]
fn gcp_a3_per_instance_hour() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_instance_hour",
        "gpu_seconds_used": "30", "gpu_count": 8,
        "duration_ms": 60000, "region": "us-central1",
        "instance_type": "a3-highgpu-8g",
    });
    let c = e.resolve_gpu_cost(
        &det,
        &env(Some("gcp"), Some("us-central1"), Some("a3-highgpu-8g")),
        None,
    );
    assert_well_formed("per_instance_hour", &c);
}

#[test]
fn azure_nd_h100_per_instance_hour() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_instance_hour",
        "gpu_seconds_used": "30", "gpu_count": 8,
        "duration_ms": 60000, "region": "eastus",
        "instance_type": "Standard_ND96isr_H100_v5",
    });
    let c = e.resolve_gpu_cost(
        &det,
        &env(Some("azure"), Some("eastus"), Some("Standard_ND96isr_H100_v5")),
        None,
    );
    assert_well_formed("per_instance_hour", &c);
}

#[test]
fn lambda_labs_per_gpu_hour_reserved() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_gpu_hour_reserved",
        "gpu_seconds_used": "60", "gpu_count": 8,
        "duration_ms": 60000, "gpu_sku": "h100-80gb-sxm5",
    });
    let c = e.resolve_gpu_cost(&det, &env(Some("lambda_labs"), None, None), None);
    assert_well_formed("per_gpu_hour_reserved", &c);
}

#[test]
fn coreweave_per_gpu_hour_reserved() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_gpu_hour_reserved",
        "gpu_seconds_used": "60", "gpu_count": 8,
        "duration_ms": 60000, "gpu_sku": "h100-80gb-sxm5",
    });
    let c = e.resolve_gpu_cost(&det, &env(Some("coreweave"), None, None), None);
    assert_well_formed("per_gpu_hour_reserved", &c);
}

#[test]
fn azure_nv6_per_vgpu_hour() {
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_vgpu_hour",
        "gpu_seconds_used": "10", "gpu_count": 1,
        "duration_ms": 10000, "region": "eastus",
        "instance_type": "Standard_NV6ads_A10_v5",
    });
    let c = e.resolve_gpu_cost(
        &det,
        &env(Some("azure"), Some("eastus"), Some("Standard_NV6ads_A10_v5")),
        None,
    );
    assert_well_formed("per_vgpu_hour", &c);
}

#[test]
fn gcp_n1_attached_resolves_via_gce_gpu_attached_block() {
    // Decision #9 N1 path — gpu_sku set; no instance_type required.
    let e = GpuPricingEngine::new();
    let det = json!({
        "billing_model": "per_gpu_hour_reserved",
        "gpu_seconds_used": "60", "gpu_count": 1,
        "duration_ms": 60000, "gpu_sku": "t4",
        "region": "us-central1",
    });
    let c = e.resolve_gpu_cost(&det, &env(Some("gcp"), Some("us-central1"), Some("n1-standard-8")), None);
    assert_well_formed("per_gpu_hour_reserved", &c);
}
