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
        let observed =
            observers.observe(case["url"].as_str().unwrap(), &headers, &case["response"]);
        if case["expected"].is_null() {
            assert!(observed.is_none(), "{name}");
            continue;
        }
        let observed = observed.unwrap_or_else(|| panic!("{name}: expected observation"));
        let expected = &case["expected"];
        assert_eq!(observed.service_key, expected["service_key"], "{name}");
        assert_eq!(observed.provider_name, expected["provider_name"], "{name}");
        assert_eq!(
            observed.provider_service, expected["provider_service"],
            "{name}"
        );
        assert_eq!(observed.component, expected["component"], "{name}");
        assert_eq!(observed.metric, expected["metric"], "{name}");
        assert_eq!(
            observed.quantity.to_string(),
            expected["quantity"],
            "{name}"
        );
        assert_eq!(
            observed.resource_id.as_deref(),
            expected["resource_id"].as_str(),
            "{name}"
        );
        assert_eq!(
            observed.provider_record_id.as_deref(),
            expected["provider_record_id"].as_str(),
            "{name}"
        );
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
