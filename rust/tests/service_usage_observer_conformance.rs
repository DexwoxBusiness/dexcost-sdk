use std::collections::HashMap;

use dexcost::pricing::service_usage_observers::default_service_usage_observers;
use serde_json::Value;

#[test]
fn shared_service_usage_observer_conformance() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../fixtures/service_usage_observation_conformance.json"
    ))
    .unwrap();
    let observers = default_service_usage_observers().expect("bundled observers must be valid");
    for case in fixture["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let headers: HashMap<String, String> =
            serde_json::from_value(case["headers"].clone()).unwrap();
        let observed = observers.observe(
            case["url"].as_str().unwrap(),
            &headers,
            &case["response"],
            case.get("request"),
        );
        let expected = case["expected"].as_array().unwrap();
        assert_eq!(observed.len(), expected.len(), "{name}");
        for (actual, wanted) in observed.iter().zip(expected) {
            assert_eq!(actual.service_key, wanted["service_key"], "{name}");
            assert_eq!(actual.provider_name, wanted["provider_name"], "{name}");
            assert_eq!(
                actual.provider_service, wanted["provider_service"],
                "{name}"
            );
            assert_eq!(actual.component, wanted["component"], "{name}");
            assert_eq!(actual.metric, wanted["metric"], "{name}");
            assert_eq!(actual.quantity.to_string(), wanted["quantity"], "{name}");
            assert_eq!(
                actual.resource_type.as_deref(),
                wanted["resource_type"].as_str(),
                "{name}"
            );
            assert_eq!(
                actual.resource_id.as_deref(),
                wanted["resource_id"].as_str(),
                "{name}"
            );
            assert_eq!(
                actual.provider_record_id.as_deref(),
                wanted["provider_record_id"].as_str(),
                "{name}"
            );
        }
    }
}

#[test]
fn packaged_observer_manifest_matches_canonical() {
    let canonical: Value =
        serde_json::from_str(include_str!("../../fixtures/service_usage_observers.json")).unwrap();
    let packaged: Value =
        serde_json::from_str(include_str!("../src/data/service_usage_observers.json")).unwrap();
    assert_eq!(packaged, canonical);
}
