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
    response_path: Option<String>,
    request_character_count_path: Option<String>,
    usage_metric: String,
    resource_type: Option<String>,
    resource_path: Option<String>,
    request_resource_path: Option<String>,
    allowed_resource_ids: Option<Vec<String>>,
    resource_query_parameter: Option<String>,
    default_resource_id: Option<String>,
    fixed_resource_id: Option<String>,
    resource_variant: Option<ResourceVariant>,
    #[serde(default)]
    query_any: Vec<QueryPredicate>,
    quantity_multiplier_path: Option<String>,
    quantity_multiplier_query_parameter: Option<String>,
    record_id_path: Option<String>,
    record_id_header: Option<String>,
    source_url: String,
}

#[derive(Clone, Deserialize)]
struct QueryPredicate {
    parameter: String,
    operator: String,
}

#[derive(Clone, Deserialize)]
struct ResourceVariant {
    query_parameter: String,
    equals: String,
    matched_suffix: String,
    default_suffix: String,
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
    pub resource_type: Option<String>,
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

fn bounded_text(value: Option<&str>) -> Option<String> {
    let value = value?.trim();
    (!value.is_empty()).then(|| value.chars().take(256).collect())
}

fn query_value_is_truthy(value: &str) -> bool {
    !matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "" | "0" | "false" | "no" | "off"
    )
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
            let has_resource_selector = observer.resource_path.is_some()
                || observer.request_resource_path.is_some()
                || observer.resource_query_parameter.is_some()
                || observer.default_resource_id.is_some()
                || observer.fixed_resource_id.is_some();
            if observer.service_key.is_empty()
                || !keys.insert(observer.service_key.clone())
                || observer.provider_name.is_empty()
                || observer.provider_service.is_empty()
                || !matches!(
                    observer.component.as_str(),
                    "external" | "speech_to_text" | "text_to_speech"
                )
                || !matches!(
                    observer.usage_metric.as_str(),
                    "input_tokens" | "audio_seconds" | "characters"
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
                || (observer.response_path.is_some()
                    == observer.request_character_count_path.is_some())
                || observer
                    .response_path
                    .as_ref()
                    .is_some_and(String::is_empty)
                || observer
                    .resource_type
                    .as_deref()
                    .is_some_and(|value| !matches!(value, "model" | "sku"))
                || (has_resource_selector && observer.resource_type.is_none())
                || observer
                    .allowed_resource_ids
                    .as_ref()
                    .is_some_and(|allowed| {
                        observer.resource_type.is_none()
                            || allowed.is_empty()
                            || allowed.iter().any(|id| id.trim().is_empty())
                    })
                || (observer.quantity_multiplier_path.is_some()
                    != observer.quantity_multiplier_query_parameter.is_some())
                || observer.query_any.iter().any(|predicate| {
                    predicate.parameter.is_empty()
                        || !matches!(predicate.operator.as_str(), "present" | "truthy")
                })
                || observer.resource_variant.as_ref().is_some_and(|variant| {
                    variant.query_parameter.is_empty()
                        || variant.equals.is_empty()
                        || variant.matched_suffix.is_empty()
                        || variant.default_suffix.is_empty()
                })
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
        request_body: Option<&serde_json::Value>,
    ) -> Vec<ServiceUsageObservation> {
        let Ok(parsed) = reqwest::Url::parse(raw_url) else {
            return Vec::new();
        };
        let query: HashMap<String, Vec<String>> =
            parsed
                .query_pairs()
                .fold(HashMap::new(), |mut values, (key, value)| {
                    values
                        .entry(key.into_owned())
                        .or_default()
                        .push(value.into_owned());
                    values
                });
        self.observers
            .iter()
            .filter(|observer| {
                observer
                    .domains
                    .iter()
                    .any(|domain| parsed.host_str() == Some(domain))
                    && observer.endpoints.iter().any(|endpoint| {
                        parsed.path() == endpoint
                            || parsed.path().starts_with(&format!("{endpoint}/"))
                    })
                    && (observer.query_any.is_empty()
                        || observer.query_any.iter().any(|predicate| {
                            query.get(&predicate.parameter).is_some_and(|values| {
                                predicate.operator == "present"
                                    || values.iter().any(|value| query_value_is_truthy(value))
                            })
                        }))
            })
            .filter_map(|observer| {
                let mut quantity =
                    if let Some(path) = observer.request_character_count_path.as_deref() {
                        let text = request_body
                            .and_then(|request| resolve_path(request, path))
                            .and_then(serde_json::Value::as_str)?;
                        let count = text.chars().count();
                        (count > 0).then(|| Decimal::from(count as u64))?
                    } else {
                        positive_decimal(resolve_path(body, observer.response_path.as_deref()?)?)?
                    };
                if let (Some(path), Some(parameter)) = (
                    observer.quantity_multiplier_path.as_deref(),
                    observer.quantity_multiplier_query_parameter.as_deref(),
                ) {
                    if query.get(parameter).is_some_and(|values| {
                        values.iter().any(|value| query_value_is_truthy(value))
                    }) {
                        if let Some(multiplier) =
                            resolve_path(body, path).and_then(positive_decimal)
                        {
                            quantity *= multiplier;
                        }
                    }
                }
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
                let mut resource_id = observer
                    .resource_path
                    .as_deref()
                    .and_then(|path| bounded_string(resolve_path(body, path)));
                if resource_id.is_none() {
                    resource_id = observer.request_resource_path.as_deref().and_then(|path| {
                        request_body.and_then(|request| bounded_string(resolve_path(request, path)))
                    });
                }
                if resource_id.is_none() {
                    resource_id =
                        observer
                            .resource_query_parameter
                            .as_deref()
                            .and_then(|parameter| {
                                query
                                    .get(parameter)
                                    .and_then(|values| values.first())
                                    .and_then(|value| bounded_text(Some(value)))
                            });
                }
                if resource_id.is_none() {
                    resource_id = bounded_text(observer.fixed_resource_id.as_deref());
                }
                if resource_id.is_none() {
                    resource_id = bounded_text(observer.default_resource_id.as_deref());
                }
                if observer
                    .allowed_resource_ids
                    .as_ref()
                    .is_some_and(|allowed| {
                        resource_id
                            .as_ref()
                            .is_none_or(|resource| !allowed.contains(resource))
                    })
                {
                    return None;
                }
                if let (Some(resource), Some(variant)) =
                    (resource_id.as_mut(), observer.resource_variant.as_ref())
                {
                    let suffix = if query
                        .get(&variant.query_parameter)
                        .and_then(|values| values.first())
                        .is_some_and(|value| value == &variant.equals)
                    {
                        &variant.matched_suffix
                    } else {
                        &variant.default_suffix
                    };
                    resource.push_str(suffix);
                    *resource = resource.chars().take(256).collect();
                }
                Some(ServiceUsageObservation {
                    service_key: observer.service_key.clone(),
                    provider_name: observer.provider_name.clone(),
                    provider_service: observer.provider_service.clone(),
                    component: observer.component.clone(),
                    metric: observer.usage_metric.clone(),
                    quantity,
                    resource_type: resource_id.as_ref().and(observer.resource_type.clone()),
                    resource_id,
                    provider_record_id,
                    manifest_version: self.version.clone(),
                })
            })
            .collect()
    }

    pub fn needs_request_body(&self, raw_url: &str) -> bool {
        let Ok(parsed) = reqwest::Url::parse(raw_url) else {
            return false;
        };
        self.observers.iter().any(|observer| {
            (observer.request_resource_path.is_some()
                || observer.request_character_count_path.is_some())
                && observer
                    .domains
                    .iter()
                    .any(|domain| parsed.host_str() == Some(domain))
                && observer.endpoints.iter().any(|endpoint| {
                    parsed.path() == endpoint || parsed.path().starts_with(&format!("{endpoint}/"))
                })
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
