use dexcost::attribution::{
    to_attribution_event_v2, to_attribution_task_ingest_v1, validate_attribution_event_v2,
    AttributionComponent, AttributionCostEvidenceSource, AttributionResourceType,
    AttributionUsageMetric, CONTRACT_VERSION,
};
use dexcost::core::models::{CostConfidence, CostEvent, EventType, PricingSource, Task};
use rust_decimal::Decimal;
use serde_json::Value;

fn usage_quantity(
    event: &dexcost::attribution::AttributionEventV2,
    metric: AttributionUsageMetric,
) -> Option<&str> {
    event
        .usage
        .iter()
        .find(|line| line.metric == metric)
        .map(|line| line.quantity.as_str())
}

#[test]
fn shared_attribution_v2_conformance() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../fixtures/attribution_v2/conformance.json"
    ))
    .expect("valid shared fixture");
    assert_eq!(fixture["contract_version"], CONTRACT_VERSION);

    for case in fixture["valid"].as_array().expect("valid cases") {
        let name = case["name"].as_str().expect("case name");
        let validation = validate_attribution_event_v2(&case["event"]);
        assert!(
            validation.success,
            "valid/{name} produced issues: {:?}",
            validation.issues
        );
    }

    for case in fixture["invalid"].as_array().expect("invalid cases") {
        let name = case["name"].as_str().expect("case name");
        let expected = case["expected_error_path"].as_str().expect("expected path");
        let validation = validate_attribution_event_v2(&case["event"]);
        assert!(!validation.success, "invalid/{name} unexpectedly passed");
        assert!(
            validation.issues.iter().any(|issue| issue.path == expected),
            "invalid/{name} missing {expected}: {:?}",
            validation.issues
        );
    }
}

#[test]
fn validator_rejects_impossible_calendar_dates() {
    let value = serde_json::json!({
        "schema_version": "2",
        "event_id": "11111111-1111-1111-1111-111111111111",
        "task_id": "22222222-2222-2222-2222-222222222222",
        "occurred_at": "2026-02-29T10:00:00Z",
        "observed_at": "2026-04-31T10:00:00Z",
        "component": "external",
        "provider": { "name": "test", "service": "api" },
        "lifecycle": { "state": "final", "revision": 1 },
        "usage": [{ "metric": "request_count", "quantity": "1", "unit": "Requests" }]
    });
    assert!(!validate_attribution_event_v2(&value).success);
}

#[test]
fn conversion_preserves_disjoint_anthropic_cache_and_reasoning() {
    let task = Task::new("test");
    let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
    event.provider = Some("anthropic".to_string());
    event.model = Some("claude-sonnet-4-5".to_string());
    event.input_tokens = Some(100);
    event.cached_tokens = Some(1000);
    event.output_tokens = Some(70);
    event
        .details
        .insert("reasoning_output_tokens".to_string(), serde_json::json!(20));
    event.cost_usd = Decimal::new(1, 2);
    event.cost_confidence = CostConfidence::Computed;
    event.pricing_source = Some(PricingSource::Litellm);
    event.pricing_version = Some("llm:test".to_string());

    let converted = to_attribution_event_v2(&event).expect("convertible event");
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::InputTokens),
        Some("100")
    );
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::CacheReadInputTokens),
        Some("1000")
    );
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::OutputTokens),
        Some("50")
    );
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::ReasoningOutputTokens),
        Some("20")
    );
    assert_eq!(
        converted.cost_evidence.expect("cost evidence").source,
        AttributionCostEvidenceSource::SdkCatalog
    );
}

#[test]
fn conversion_preserves_rate_registry_quantity_and_version() {
    let task = Task::new("test");
    let mut event = CostEvent::new(&task.task_id, EventType::ExternalCost);
    event.service_name = Some("ocr".to_string());
    event.cost_usd = Decimal::from(5);
    event.cost_confidence = CostConfidence::Computed;
    event.pricing_source = Some(PricingSource::RateRegistry);
    event.pricing_version = Some("rates:test".to_string());
    event.details.insert(
        "attribution_usage_quantity".to_string(),
        serde_json::json!(25),
    );
    event.details.insert(
        "attribution_usage_per".to_string(),
        serde_json::json!("page"),
    );

    let converted = to_attribution_event_v2(&event).expect("convertible event");
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::PageCount),
        Some("25")
    );
    assert_eq!(
        converted.cost_evidence.expect("cost evidence").source,
        AttributionCostEvidenceSource::SdkRateRegistry
    );
}

#[test]
fn conversion_preserves_retry_marker_linkage_reason_and_cost() {
    let task = Task::new("test");
    let mut event = CostEvent::new(&task.task_id, EventType::RetryMarker);
    event.model = Some("gpt-5".to_string());
    event.is_retry = true;
    event.retry_reason = Some("rate_limit".to_string());
    event.retry_of = Some("33333333-3333-4333-8333-333333333333".to_string());
    event.cost_usd = Decimal::new(2, 2);

    let converted = to_attribution_event_v2(&event).expect("convertible retry marker");
    assert_eq!(converted.component, AttributionComponent::External);
    assert_eq!(converted.provider.name, "dexcost");
    assert_eq!(converted.provider.service, "retry");
    assert_eq!(
        usage_quantity(&converted, AttributionUsageMetric::RequestCount),
        Some("1")
    );
    let resource = converted.resource.expect("retry reason resource");
    assert_eq!(resource.resource_type, AttributionResourceType::Other);
    assert_eq!(resource.id, "rate_limit");
    assert_eq!(
        converted.retry_of.as_deref(),
        Some("33333333-3333-4333-8333-333333333333")
    );
    let evidence = converted.cost_evidence.expect("manual retry cost");
    assert_eq!(evidence.source, AttributionCostEvidenceSource::Manual);
    assert_eq!(evidence.amount, "0.02");
}

#[test]
fn conversion_closes_time_based_compute_and_gpu_usage() {
    let task = Task::new("test");
    for (event_type, details, metric) in [
        (
            EventType::ComputeCost,
            serde_json::json!({"vcpu_seconds_used": 2.5}),
            AttributionUsageMetric::VcpuSeconds,
        ),
        (
            EventType::GpuCost,
            serde_json::json!({"gpu_seconds_used": 3, "billing_model": "per_gpu_second_active"}),
            AttributionUsageMetric::GpuSeconds,
        ),
    ] {
        let mut event = CostEvent::new(&task.task_id, event_type);
        event.details = details
            .as_object()
            .expect("details")
            .clone()
            .into_iter()
            .collect();
        let converted = to_attribution_event_v2(&event).expect("convertible event");
        assert!(usage_quantity(&converted, metric).is_some());
        assert!(
            converted
                .usage_period
                .and_then(|period| period.end_at)
                .is_some(),
            "time-based usage must have a closed period"
        );
    }
}

#[test]
fn task_ingest_excludes_aggregate_costs_and_event_drops_details() {
    let mut task = Task::new("support");
    task.total_cost_usd = Decimal::from(99);
    let task_value = serde_json::to_value(to_attribution_task_ingest_v1(&task)).unwrap();
    for forbidden in [
        "total_cost_usd",
        "llm_cost_usd",
        "external_cost_usd",
        "total_input_tokens",
    ] {
        assert!(task_value.get(forbidden).is_none(), "leaked {forbidden}");
    }

    let mut event = CostEvent::new(&task.task_id, EventType::ExternalCost);
    event
        .details
        .insert("secret".to_string(), serde_json::json!("nope"));
    let event_value =
        serde_json::to_value(to_attribution_event_v2(&event).expect("convertible event")).unwrap();
    assert!(event_value.get("details").is_none());

    let signal = CostEvent::new(&task.task_id, EventType::GpuUtilizationSignal);
    assert!(to_attribution_event_v2(&signal).is_none());
}
