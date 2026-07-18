//! Provider-owned usage observations for services withheld from SDK pricing.

use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::OnceLock;

use rust_decimal::Decimal;
use serde::Deserialize;

const MANIFEST_JSON: &str = include_str!("../data/service_usage_observers.json");

#[derive(Clone, Deserialize)]
struct ObserverDefinition {
    service_key: String,
    provider_name: String,
    provider_service: String,
    component: String,
    domains: Vec<String>,
    endpoints: Vec<String>,
    response_path: String,
    usage_metric: String,
    resource_path: Option<String>,
    record_id_path: Option<String>,
    record_id_header: Option<String>,
    source_url: String,
}

#[derive(Deserialize)]
struct ManifestMeta {
    version: String,
    observer_count: usize,
}

#[derive(Deserialize)]
struct Manifest {
    #[serde(rename = "_meta")]
    meta: ManifestMeta,
    observers: Vec<ObserverDefinition>,
}

pub struct ServiceUsageObservers {
    version: String,
    observers: Vec<ObserverDefinition>,
}

pub struct ServiceUsageObservation {
    pub service_key: String,
    pub provider_name: String,
    pub provider_service: String,
    pub component: String,
    pub metric: String,
    pub quantity: Decimal,
    pub resource_id: Option<String>,
    pub provider_record_id: Option<String>,
    pub manifest_version: String,
}

fn resolve_path<'a>(value: &'a serde_json::Value, path: &str) -> Option<&'a serde_json::Value> {
    let mut current = value;
    for part in path.split('.') {
        current = current.get(part)?;
    }
    Some(current)
}

fn positive_decimal(value: &serde_json::Value) -> Option<Decimal> {
    let raw = match value {
        serde_json::Value::String(value) => value.clone(),
        serde_json::Value::Number(value) => value.to_string(),
        _ => return None,
    };
    let parsed = Decimal::from_str(&raw).ok()?;
    (parsed > Decimal::ZERO).then_some(parsed)
}

fn bounded_string(value: Option<&serde_json::Value>) -> Option<String> {
    let value = value?.as_str()?.trim();
    if value.is_empty() {
        return None;
    }
    Some(value.chars().take(256).collect())
}

impl ServiceUsageObservers {
    fn load() -> Option<Self> {
        let manifest: Manifest = serde_json::from_str(MANIFEST_JSON).ok()?;
        if manifest.meta.version.is_empty()
            || manifest.meta.observer_count != manifest.observers.len()
        {
            return None;
        }
        let mut keys = HashSet::new();
        for observer in &manifest.observers {
            if observer.service_key.is_empty()
                || !keys.insert(observer.service_key.clone())
                || observer.provider_name.is_empty()
                || observer.provider_service.is_empty()
                || !matches!(observer.component.as_str(), "external" | "speech_to_text")
                || !matches!(
                    observer.usage_metric.as_str(),
                    "input_tokens" | "audio_seconds"
                )
                || observer.domains.is_empty()
                || observer.endpoints.is_empty()
                || observer
                    .domains
                    .iter()
                    .any(|domain| domain.trim().is_empty())
                || observer
                    .endpoints
                    .iter()
                    .any(|endpoint| !endpoint.starts_with('/'))
                || observer.response_path.is_empty()
                || !observer.source_url.starts_with("https://")
            {
                return None;
            }
        }
        Some(Self {
            version: manifest.meta.version,
            observers: manifest.observers,
        })
    }

    pub fn observe(
        &self,
        raw_url: &str,
        headers: &HashMap<String, String>,
        body: &serde_json::Value,
    ) -> Option<ServiceUsageObservation> {
        let parsed = reqwest::Url::parse(raw_url).ok()?;
        let observer = self.observers.iter().find(|observer| {
            observer
                .domains
                .iter()
                .any(|domain| parsed.host_str() == Some(domain))
                && observer.endpoints.iter().any(|endpoint| {
                    parsed.path() == endpoint || parsed.path().starts_with(&format!("{endpoint}/"))
                })
        })?;
        let quantity = positive_decimal(resolve_path(body, &observer.response_path)?)?;
        let mut provider_record_id = observer
            .record_id_path
            .as_deref()
            .and_then(|path| bounded_string(resolve_path(body, path)));
        if provider_record_id.is_none() {
            if let Some(header) = observer.record_id_header.as_deref() {
                provider_record_id = headers
                    .iter()
                    .find(|(key, _)| key.eq_ignore_ascii_case(header))
                    .map(|(_, value)| value.trim())
                    .filter(|value| !value.is_empty())
                    .map(|value| value.chars().take(256).collect());
            }
        }
        let resource_id = observer
            .resource_path
            .as_deref()
            .and_then(|path| bounded_string(resolve_path(body, path)));
        Some(ServiceUsageObservation {
            service_key: observer.service_key.clone(),
            provider_name: observer.provider_name.clone(),
            provider_service: observer.provider_service.clone(),
            component: observer.component.clone(),
            metric: observer.usage_metric.clone(),
            quantity,
            resource_id,
            provider_record_id,
            manifest_version: self.version.clone(),
        })
    }
}

pub fn default_service_usage_observers() -> Option<&'static ServiceUsageObservers> {
    static OBSERVERS: OnceLock<Option<ServiceUsageObservers>> = OnceLock::new();
    OBSERVERS
        .get_or_init(|| {
            let observers = ServiceUsageObservers::load();
            if observers.is_none() {
                eprintln!("[dexcost] bundled service usage observers disabled: invalid manifest");
            }
            observers
        })
        .as_ref()
}
