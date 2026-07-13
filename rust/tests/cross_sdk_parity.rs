// Cross-SDK parity test for the Rust SDK.
//
// Consumes the canonical fixture corpus at <repo>/fixtures/ produced by
// python/tests/test_cross_sdk_parity.py. Asserts the Rust SDK round-trips
// events / tasks and produces pricing output that matches the Python-
// canonical expected outputs.
//
// This suite is intentionally RED on initial commit. Each failing test
// pins an audit finding scheduled for Sprint 1+:
//   - B5  ec2 / k8s_pod compute discriminator
//   - B6  schema enum gap (downstream)
//   - Rust total_cost_usd clobber (network + gpu not summed)
//   - P1  occurred_at timestamp format drift
//   - P2  PricingSource enum spelling drift
//   - URL scrubber absent in Rust (Theme A, Sprint 1)
//
// Run: cargo test --test cross_sdk_parity

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use rust_decimal::Decimal;
use serde_json::Value;

use dexcost::{scrub_url, CostEvent, PricingEngine, Task};

fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("fixtures")
}

fn read_json(path: &Path) -> Value {
    let raw = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("parse {}: {}", path.display(), e))
}

fn strip_underscored(v: Value) -> Value {
    match v {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, vv) in map {
                if k.starts_with('_') {
                    continue;
                }
                out.insert(k, strip_underscored(vv));
            }
            Value::Object(out)
        }
        other => other,
    }
}

fn expected_path_for(rel: &str, kind: &str) -> PathBuf {
    let base = fixtures_dir().join("expected_outputs").join(kind);
    if let Some(rest) = rel.strip_prefix("pricing_inputs/") {
        base.join(rest)
    } else if rel.contains("edge_cases/") {
        let name = Path::new(rel).file_name().unwrap();
        base.join("edge_cases").join(name)
    } else if let Some(rest) = rel.strip_prefix("tasks/") {
        base.join("tasks").join(rest)
    } else if let Some(rest) = rel.strip_prefix("events/") {
        base.join(rest)
    } else {
        base.join(rel)
    }
}

/// Compare two serde Values with stable key ordering. Returns a unified diff-ish
/// message on inequality.
fn assert_value_eq(label: &str, expected: &Value, actual: &Value) {
    if expected == actual {
        return;
    }
    let exp = serde_json::to_string_pretty(&sorted(expected.clone())).unwrap();
    let act = serde_json::to_string_pretty(&sorted(actual.clone())).unwrap();
    panic!(
        "{} canonical-serialization drift\n--- expected ---\n{}\n--- actual ---\n{}",
        label, exp, act
    );
}

fn sorted(v: Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut bt = BTreeMap::new();
            for (k, vv) in m {
                bt.insert(k, sorted(vv));
            }
            let mut out = serde_json::Map::new();
            for (k, vv) in bt {
                out.insert(k, vv);
            }
            Value::Object(out)
        }
        Value::Array(a) => Value::Array(a.into_iter().map(sorted).collect()),
        other => other,
    }
}

const EVENT_FIXTURES: &[&str] = &[
    "events/llm_call.v1.json",
    "events/external_cost.v1.json",
    "events/compute_cost_lambda.v1.json",
    "events/compute_cost_ec2_share.v1.json",
    "events/compute_cost_k8s_pod.v1.json",
    "events/network.v1.json",
    "events/network_4xx_below_threshold.v1.json",
    "events/gpu_cost.v1.json",
    "events/gpu_utilization_signal.v1.json",
    "events/retry_marker.v1.json",
    "events/edge_cases/tiny_decimal.v1.json",
];

const TASK_FIXTURES: &[&str] = &[
    "tasks/task_minimal.v1.json",
    "tasks/task_with_network_gpu.v1.json",
];

#[test]
fn cross_sdk_event_canonical_serialization() {
    let mut failures: Vec<String> = Vec::new();
    for rel in EVENT_FIXTURES {
        let input = strip_underscored(read_json(&fixtures_dir().join(rel)));
        let expected = read_json(&expected_path_for(rel, "canonical_serialization"));
        let evt: CostEvent = match serde_json::from_value(input.clone()) {
            Ok(e) => e,
            Err(e) => {
                failures.push(format!("{}: deserialize failed: {}", rel, e));
                continue;
            }
        };
        let actual = evt.to_dict();
        if sorted(expected.clone()) != sorted(actual.clone()) {
            let exp = serde_json::to_string_pretty(&sorted(expected)).unwrap();
            let act = serde_json::to_string_pretty(&sorted(actual)).unwrap();
            failures.push(format!(
                "{}: drift\n--- expected ---\n{}\n--- actual ---\n{}",
                rel, exp, act
            ));
        }
    }
    if !failures.is_empty() {
        panic!(
            "{} of {} event fixtures failed:\n{}",
            failures.len(),
            EVENT_FIXTURES.len(),
            failures.join("\n\n")
        );
    }
}

#[test]
fn cross_sdk_task_canonical_serialization() {
    let mut failures: Vec<String> = Vec::new();
    for rel in TASK_FIXTURES {
        let input = strip_underscored(read_json(&fixtures_dir().join(rel)));
        let expected = read_json(&expected_path_for(rel, "canonical_serialization"));
        let task: Task = match serde_json::from_value(input.clone()) {
            Ok(t) => t,
            Err(e) => {
                failures.push(format!("{}: deserialize failed: {}", rel, e));
                continue;
            }
        };
        let actual = task.to_dict();
        if sorted(expected.clone()) != sorted(actual.clone()) {
            let exp = serde_json::to_string_pretty(&sorted(expected)).unwrap();
            let act = serde_json::to_string_pretty(&sorted(actual)).unwrap();
            failures.push(format!(
                "{}: drift\n--- expected ---\n{}\n--- actual ---\n{}",
                rel, exp, act
            ));
        }
    }
    if !failures.is_empty() {
        panic!(
            "{} of {} task fixtures failed:\n{}",
            failures.len(),
            TASK_FIXTURES.len(),
            failures.join("\n\n")
        );
    }
}

#[test]
fn cross_sdk_llm_pricing_parity() {
    let engine = PricingEngine::new();
    for rel in &[
        "pricing_inputs/llm/gpt4o_500_in_200_out.json",
        "pricing_inputs/llm/claude_sonnet_streaming_2000_in_1500_out.json",
        "pricing_inputs/llm/claude_cache_read_disjoint.json",
        "pricing_inputs/llm/claude_cache_write_disjoint.json",
    ] {
        let input = strip_underscored(read_json(&fixtures_dir().join(rel)));
        let expected = read_json(&expected_path_for(rel, "pricing"));
        let model = input["model"].as_str().expect("model");
        let in_tok = input["input_tokens"].as_i64().expect("input_tokens");
        let out_tok = input["output_tokens"].as_i64().expect("output_tokens");
        let cached = input
            .get("cached_tokens")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let cache_creation = input
            .get("cache_creation_tokens")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let actual = engine.get_cost_sync(model, in_tok, out_tok, cached, cache_creation);
        let expected_cost: Decimal = expected["cost_usd"]
            .as_str()
            .expect("expected cost_usd")
            .parse()
            .expect("parse expected cost_usd");
        assert_eq!(
            actual.cost_usd, expected_cost,
            "{}: LLM cost drift expected={} actual={}",
            rel, expected_cost, actual.cost_usd
        );
    }
    let _ = assert_value_eq; // silence unused warning when only this test runs
}

#[test]
fn cross_sdk_compute_dispatch_b5() {
    // Sprint 1 / Theme F / §2.3.1 (B5) regression — discriminator only.
    //
    // Pre-fix Rust dispatched on "ec2_share" / "gce_share" /
    // "azure_vm_share" / "k8s_pod_share" while Python + Go + the
    // canonical Sprint 0 fixtures all use "ec2" / "gce" / "azure_vm" /
    // "k8s_pod". Result: Rust silently hit the `_ => zero_unknown`
    // fallback and reported $0 for any IaaS-share compute workload.
    //
    // This test asserts the dispatch now REACHES the IaaS / k8s share
    // pricing path (pricing_source != "compute_catalog:unknown" AND
    // cost > 0). It does NOT assert exact-cost parity yet — the
    // cross-SDK fixture surfaced a SECOND, previously un-audited bug
    // where Rust's Tier 4 IaaS fallback math diverges from Python by a
    // factor of vcpu_count (Python uses share_factor × instance_hourly,
    // Rust uses vcpu_seconds/3600 × per_vcpu). That math bug is a
    // separate finding — tracked as B5b in PARITY-AUDIT.md; needs its
    // own commit so the scope stays clean. Per plan §7 (fixture suite
    // reveals new bugs → pause, audit, add to plan).
    use dexcost::cloud_detect::CloudEnv;
    let engine = dexcost::pricing::compute_pricing::ComputePricingEngine::new();
    let cloud_env = CloudEnv::none();
    let overrides = std::collections::HashMap::new();

    for rel in &[
        "pricing_inputs/compute/ec2_share_4vcpu_8gb_3600s.json",
        "pricing_inputs/compute/k8s_pod_2vcpu_4gb_1800s.json",
    ] {
        let input = strip_underscored(read_json(&fixtures_dir().join(rel)));
        let window_s = input
            .get("duration_ms")
            .and_then(|v| v.as_f64())
            .map(|ms| Decimal::from_f64_retain(ms / 1000.0).unwrap_or(Decimal::ZERO));

        let actual = engine.resolve_compute_cost(&input, &cloud_env, &overrides, window_s);

        assert_ne!(
            actual.pricing_source, "compute_catalog:unknown",
            "{}: dispatch did not reach IaaS/k8s pricing path \
             (billing_model={}, pricing_source={})",
            rel, input["billing_model"], actual.pricing_source,
        );
        assert!(
            actual.cost_usd > Decimal::ZERO,
            "{}: expected non-zero cost after B5 discriminator fix, got {}",
            rel,
            actual.cost_usd,
        );
    }
}

#[test]
fn cross_sdk_compute_pricing_exact_parity_b5b() {
    // The strict assertion the plan §2.3.1 asked for — exact cost
    // parity vs Python across all four SDKs. Post-B5b: Rust now mirrors
    // Python's _iaas_share formula (vcpu_seconds / (vcpu_count × 3600)
    // × instance_hourly).
    use dexcost::cloud_detect::CloudEnv;
    let engine = dexcost::pricing::compute_pricing::ComputePricingEngine::new();
    let cloud_env = CloudEnv::none();
    let overrides = std::collections::HashMap::new();

    for rel in &[
        "pricing_inputs/compute/ec2_share_4vcpu_8gb_3600s.json",
        "pricing_inputs/compute/k8s_pod_2vcpu_4gb_1800s.json",
    ] {
        let input = strip_underscored(read_json(&fixtures_dir().join(rel)));
        let expected = read_json(&expected_path_for(rel, "pricing"));
        let window_s = input
            .get("duration_ms")
            .and_then(|v| v.as_f64())
            .map(|ms| Decimal::from_f64_retain(ms / 1000.0).unwrap_or(Decimal::ZERO));

        let actual = engine.resolve_compute_cost(&input, &cloud_env, &overrides, window_s);
        let expected_cost: Decimal = expected["cost_usd"]
            .as_str()
            .expect("expected cost_usd")
            .parse()
            .expect("parse expected cost_usd");

        assert_eq!(
            actual.cost_usd, expected_cost,
            "{}: compute cost drift\n  expected={}\n  actual={}",
            rel, expected_cost, actual.cost_usd,
        );
    }
}

#[test]
fn cross_sdk_url_scrubber_parity() {
    // Sprint 1 / Theme A. Asserts dexcost::scrub_url matches the canonical
    // Python implementation byte-for-byte against the shared fixtures.
    let url_fixtures = [
        "events/edge_cases/url_with_basic_auth.v1.json",
        "events/edge_cases/url_with_api_key_query.v1.json",
        "events/edge_cases/url_with_signed_s3.v1.json",
    ];
    let mut failures = Vec::new();
    for rel in &url_fixtures {
        let input = read_json(&fixtures_dir().join(rel));
        let raw_url = input["_test_input"]["url"]
            .as_str()
            .expect("missing _test_input.url");
        let expected = read_json(
            &fixtures_dir()
                .join("expected_outputs/security")
                .join(Path::new(rel).file_name().unwrap()),
        );
        let expected_raw = expected["raw_url"].as_str().unwrap();
        let expected_scrubbed = expected["scrubbed_url"].as_str().unwrap();
        if expected_raw != raw_url {
            failures.push(format!("{rel}: raw_url mismatch"));
            continue;
        }
        let actual = scrub_url(raw_url);
        if actual != expected_scrubbed {
            failures.push(format!(
                "{rel}: drift\n  raw:      {raw_url}\n  expected: {expected_scrubbed}\n  actual:   {actual}"
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}

/// B3 invariant: summing 1.23E-8 ten thousand times must equal 0.0001230000 exactly.
#[test]
fn cross_sdk_tiny_decimal_accumulation() {
    let expected = read_json(
        &fixtures_dir().join("expected_outputs/pricing/decimal_accumulation_invariant.json"),
    );
    // Accept both "1.23E-8" (Python repr) and "0.0000000123" (canonical form).
    // rust_decimal's FromStr does not accept E-notation, so try from_scientific
    // first, falling back to from_str.
    let per_str = expected["per_event_cost_usd"].as_str().unwrap();
    let per: Decimal = Decimal::from_scientific(per_str)
        .or_else(|_| per_str.parse::<Decimal>())
        .unwrap_or_else(|e| panic!("parse per_event_cost_usd {}: {}", per_str, e));
    let iters = expected["iterations"].as_u64().unwrap();
    let want: Decimal = expected["total_cost_usd"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();

    let mut total = Decimal::ZERO;
    for _ in 0..iters {
        total += per;
    }
    assert_eq!(
        total, want,
        "decimal accumulation drift: {} x {} = {}, expected {}",
        iters, per, total, want
    );
}
