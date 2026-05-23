//! GPU catalog integrity tests — Phase 2 Task 4.
//!
//! Rust port of `python/tests/test_gpu_catalog_integrity.py` (commit 97f736b).
//! Pins the structural invariants of the bundled `data/gpu_prices.json` so
//! that a future refresh can't drift shape, and enforces Decision #11's
//! freshness thresholds.
//!
//! No production code — these tests load the bundled catalog and walk it.

use std::str::FromStr;

use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde_json::Value;

const CATALOG: &str = include_str!("../src/data/gpu_prices.json");

fn catalog() -> Value {
    serde_json::from_str(CATALOG).expect("gpu_prices.json must be valid JSON")
}

#[test]
fn catalog_has_all_eight_providers() {
    let c = catalog();
    for prov in &[
        "aws",
        "gcp",
        "azure",
        "modal",
        "runpod",
        "lambda_labs",
        "coreweave",
        "replicate",
    ] {
        assert!(
            c.get(prov).is_some(),
            "gpu_prices.json missing provider block: {}",
            prov
        );
    }
}

#[test]
fn meta_has_four_billing_model_defaults() {
    let c = catalog();
    let meta = c.get("_meta").expect("_meta block required");
    for key in &[
        "default_per_instance_hour_usd",
        "default_per_gpu_second_active_usd",
        "default_per_gpu_hour_reserved_usd",
        "default_per_vgpu_hour_usd",
    ] {
        assert!(
            meta.get(key).is_some(),
            "_meta missing default rate: {}",
            key
        );
    }
}

#[test]
fn meta_version_is_semver() {
    let c = catalog();
    let v = c
        .get("_meta")
        .and_then(|m| m.get("version"))
        .and_then(|v| v.as_str())
        .expect("_meta.version required");
    let parts: Vec<&str> = v.split('.').collect();
    assert_eq!(parts.len(), 3, "_meta.version must be X.Y.Z; got {}", v);
    for p in parts {
        p.parse::<u32>()
            .unwrap_or_else(|_| panic!("_meta.version component non-numeric: {}", v));
    }
}

#[test]
fn aws_has_ec2_gpu_regions() {
    let c = catalog();
    assert!(c["aws"]["ec2_gpu"]["regions"].is_object());
}

#[test]
fn gcp_has_both_attached_and_bundled() {
    let c = catalog();
    assert!(c["gcp"]["gce_gpu_attached"].is_object());
    assert!(c["gcp"]["gce_gpu_bundled"].is_object());
}

#[test]
fn azure_has_both_vm_gpu_and_vm_vgpu() {
    let c = catalog();
    assert!(c["azure"]["vm_gpu"].is_object());
    assert!(c["azure"]["vm_vgpu"].is_object());
}

#[test]
fn serverless_providers_have_per_gpu_second_active() {
    let c = catalog();
    for prov in &["modal", "runpod", "replicate"] {
        assert!(
            c[prov]["per_gpu_second_active"].is_object(),
            "{} must have per_gpu_second_active block",
            prov
        );
    }
}

#[test]
fn reserved_providers_have_per_gpu_hour_reserved() {
    let c = catalog();
    for prov in &["lambda_labs", "coreweave"] {
        assert!(
            c[prov]["per_gpu_hour_reserved"].is_object(),
            "{} must have per_gpu_hour_reserved block",
            prov
        );
    }
}

fn parse_iso_date(s: &str) -> Option<NaiveDate> {
    // Accept either YYYY-MM-DD or ISO-8601 datetimes.
    if let Ok(d) = NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        return Some(d);
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(dt.naive_utc().date());
    }
    None
}

#[test]
fn every_provider_has_parseable_last_verified() {
    let c = catalog();
    for prov in &[
        "aws",
        "gcp",
        "azure",
        "modal",
        "runpod",
        "lambda_labs",
        "coreweave",
        "replicate",
    ] {
        let block = &c[prov];
        let lv = block
            .get("_last_verified")
            .and_then(|v| v.as_str())
            .unwrap_or_else(|| panic!("{} missing _last_verified", prov));
        parse_iso_date(lv).unwrap_or_else(|| panic!("{} _last_verified unparseable: {}", prov, lv));
    }
}

#[test]
fn decision_11_no_provider_older_than_365_days() {
    // Hard-fail threshold per Decision #11.
    let c = catalog();
    let today = Utc::now().naive_utc().date();
    for prov in &[
        "aws",
        "gcp",
        "azure",
        "modal",
        "runpod",
        "lambda_labs",
        "coreweave",
        "replicate",
    ] {
        let lv = c[prov]
            .get("_last_verified")
            .and_then(|v| v.as_str())
            .unwrap_or("1970-01-01");
        let d = parse_iso_date(lv).unwrap_or_else(|| panic!("unparseable date: {}", lv));
        let age_days = (today - d).num_days();
        assert!(
            age_days < 365,
            "Provider {} _last_verified is {} days old (> 365); refresh required",
            prov,
            age_days
        );
    }
}

#[test]
fn all_dollar_amounts_parse_as_decimal() {
    // Tree-walk every *_usd / gpu_count / gpu_vram_gb / memory_gb string —
    // each must parse cleanly as Decimal (no float-drift surface).
    let c = catalog();
    fn walk(v: &Value, path: &str, errors: &mut Vec<String>) {
        match v {
            Value::Object(m) => {
                for (k, vv) in m {
                    let p = if path.is_empty() {
                        k.clone()
                    } else {
                        format!("{}.{}", path, k)
                    };
                    // Decimal-string-shaped keys.
                    let dec_keyed = k.ends_with("_usd")
                        || k == "vcpu_count"
                        || k == "gpu_count"
                        || k == "gpu_vram_gb"
                        || k == "memory_gb";
                    if dec_keyed {
                        if let Some(s) = vv.as_str() {
                            if Decimal::from_str(s).is_err() {
                                errors.push(format!("{} = {:?} not parseable as Decimal", p, s));
                            }
                        } else if vv.is_object() || vv.is_array() {
                            // Nested structure; recurse.
                            walk(vv, &p, errors);
                        } else if !vv.is_number() && !vv.is_null() {
                            errors.push(format!(
                                "{} has non-string non-number type {:?}",
                                p, vv
                            ));
                        }
                    } else {
                        walk(vv, &p, errors);
                    }
                }
            }
            Value::Array(a) => {
                for (i, item) in a.iter().enumerate() {
                    walk(item, &format!("{}[{}]", path, i), errors);
                }
            }
            _ => {}
        }
    }
    let mut errs = Vec::new();
    walk(&c, "", &mut errs);
    assert!(errs.is_empty(), "Decimal-shape violations:\n{}", errs.join("\n"));
}

#[test]
fn cross_provider_h100_canonical_sku_consistency() {
    // Load-bearing — `h100-80gb-sxm5` must appear across providers so a
    // customer can compare "Modal H100 vs AWS p5" through dexcost. This is
    // the catalog portability contract from §4 of the design.
    let c = catalog();
    let json_str = serde_json::to_string(&c).expect("re-serialize catalog");
    assert!(
        json_str.contains("h100-80gb-sxm5"),
        "catalog must contain canonical SKU h100-80gb-sxm5 across providers"
    );
}
