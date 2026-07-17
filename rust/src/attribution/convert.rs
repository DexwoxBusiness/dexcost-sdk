use std::str::FromStr;

use chrono::{Duration, SecondsFormat};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde_json::Value;

use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource, Task, TaskStatus};

use super::types::*;
use super::validate::validate_attribution_event_v2;

const GIB: i64 = 1024 * 1024 * 1024;

pub fn to_attribution_event_v2(event: &CostEvent) -> Option<AttributionEventV2> {
    let (component, mut usage, duration) = component_and_usage(event)?;
    if usage.is_empty() {
        append_usage(
            &mut usage,
            AttributionUsageMetric::RequestCount,
            Decimal::ONE,
        );
    }

    let occurred_at = canonical_time(event.occurred_at);
    let has_time_usage = usage.iter().any(|line| {
        matches!(
            line.unit,
            AttributionUsageUnit::Seconds
                | AttributionUsageUnit::VcpuSeconds
                | AttributionUsageUnit::GibSeconds
                | AttributionUsageUnit::GpuSeconds
        )
    });
    let usage_period = if has_time_usage || duration > Decimal::ZERO {
        let micros = duration
            .max(Decimal::ZERO)
            .checked_mul(Decimal::from(1_000_000_i64))
            .and_then(|value| value.round().to_i64())
            .unwrap_or(0);
        Some(AttributionUsagePeriodV2 {
            start_at: canonical_time(event.occurred_at - Duration::microseconds(micros)),
            end_at: Some(occurred_at.clone()),
        })
    } else {
        None
    };

    let converted = AttributionEventV2 {
        schema_version: "2".to_string(),
        event_id: event.event_id.clone(),
        task_id: event.task_id.clone(),
        occurred_at: occurred_at.clone(),
        observed_at: occurred_at,
        component,
        provider: provider_for(event),
        resource: resource_for(event),
        lifecycle: AttributionLifecycleV2 {
            state: AttributionLifecycleState::Final,
            revision: 1,
        },
        usage_period,
        usage,
        cost_evidence: cost_evidence_for(event),
        retry_of: if event.is_retry {
            event.retry_of.clone()
        } else {
            None
        },
    };

    let value = serde_json::to_value(&converted).ok()?;
    let validation = validate_attribution_event_v2(&value);
    if validation.success {
        Some(converted)
    } else {
        let paths: Vec<&str> = validation
            .issues
            .iter()
            .map(|issue| issue.path.as_str())
            .collect();
        eprintln!(
            "[dexcost] event {} cannot be represented by attribution v2: {}",
            event.event_id,
            paths.join(", ")
        );
        None
    }
}

pub fn to_attribution_task_ingest_v1(task: &Task) -> AttributionTaskIngestV1 {
    AttributionTaskIngestV1 {
        task_id: task.task_id.clone(),
        task_type: task.task_type.clone(),
        status: match task.status {
            TaskStatus::Running => "running",
            TaskStatus::Pending => "pending",
            TaskStatus::Success => "success",
            TaskStatus::Failed => "failed",
        }
        .to_string(),
        started_at: canonical_time(task.started_at),
        ended_at: task.ended_at.map(canonical_time),
        metadata: task.metadata.clone(),
        customer_id: task.customer_id.clone(),
        project_id: task.project_id.clone(),
        parent_task_id: task.parent_task_id.clone(),
        experiment_id: task.experiment_id.clone(),
        variant: task.variant.clone(),
        schema_version: "1".to_string(),
    }
}

fn component_and_usage(
    event: &CostEvent,
) -> Option<(AttributionComponent, Vec<AttributionUsageLineV2>, Decimal)> {
    let mut usage = Vec::new();
    match event.event_type {
        EventType::RetryMarker | EventType::GpuUtilizationSignal => None,
        EventType::LlmCall => {
            let cached = Decimal::from(event.cached_tokens.unwrap_or(0).max(0));
            let mut input = Decimal::from(event.input_tokens.unwrap_or(0).max(0));
            let provider = event
                .provider
                .as_deref()
                .unwrap_or_default()
                .to_ascii_lowercase();
            if !provider.contains("anthropic") && !provider.contains("bedrock") && provider != "aws"
            {
                input = (input - cached).max(Decimal::ZERO);
            }
            let cache_write = decimal_detail(&event.details, &["cache_creation_input_tokens"])
                .unwrap_or(Decimal::ZERO);
            let reasoning = decimal_detail(
                &event.details,
                &["reasoning_output_tokens", "reasoning_tokens"],
            )
            .unwrap_or(Decimal::ZERO);
            let output = (Decimal::from(event.output_tokens.unwrap_or(0).max(0)) - reasoning)
                .max(Decimal::ZERO);
            append_usage(&mut usage, AttributionUsageMetric::InputTokens, input);
            append_usage(
                &mut usage,
                AttributionUsageMetric::CacheReadInputTokens,
                cached,
            );
            append_usage(
                &mut usage,
                AttributionUsageMetric::CacheWriteInputTokens,
                cache_write,
            );
            append_usage(&mut usage, AttributionUsageMetric::OutputTokens, output);
            append_usage(
                &mut usage,
                AttributionUsageMetric::ReasoningOutputTokens,
                reasoning,
            );
            Some((AttributionComponent::Llm, usage, Decimal::ZERO))
        }
        EventType::ComputeCost => {
            let duration = decimal_detail(&event.details, &["duration_ms"])
                .unwrap_or(Decimal::ZERO)
                / Decimal::from(1000_i64);
            let duration = if duration.is_zero() {
                decimal_detail(&event.details, &["wall_clock_seconds"]).unwrap_or(Decimal::ZERO)
            } else {
                duration
            };
            append_usage(&mut usage, AttributionUsageMetric::ComputeSeconds, duration);
            append_usage(
                &mut usage,
                AttributionUsageMetric::VcpuSeconds,
                decimal_detail(&event.details, &["vcpu_seconds_used"]).unwrap_or(Decimal::ZERO),
            );
            if let Some(memory) =
                decimal_detail(&event.details, &["memory_bytes_limit", "memory_bytes_peak"])
            {
                append_usage(
                    &mut usage,
                    AttributionUsageMetric::MemoryGibSeconds,
                    memory / Decimal::from(GIB) * duration,
                );
            }
            append_usage(
                &mut usage,
                AttributionUsageMetric::RequestCount,
                decimal_detail(&event.details, &["invocation_count"]).unwrap_or(Decimal::ZERO),
            );
            Some((AttributionComponent::Compute, usage, duration))
        }
        EventType::GpuCost => {
            let duration = decimal_detail(&event.details, &["duration_ms"])
                .unwrap_or(Decimal::ZERO)
                / Decimal::from(1000_i64);
            let measured = decimal_detail(&event.details, &["gpu_seconds_used"]);
            let count = decimal_detail(&event.details, &["gpu_count"]).unwrap_or(Decimal::ONE);
            let mut billed = duration * count;
            if (string_detail(&event.details, &["billing_model"]) == Some("per_gpu_second_active")
                || billed.is_zero())
                && measured.is_some()
            {
                billed = measured.unwrap_or(Decimal::ZERO);
            }
            append_usage(&mut usage, AttributionUsageMetric::GpuSeconds, billed);
            Some((AttributionComponent::Gpu, usage, duration))
        }
        EventType::Network => {
            append_usage(
                &mut usage,
                AttributionUsageMetric::BytesOut,
                decimal_detail(&event.details, &["request_bytes"]).unwrap_or(Decimal::ZERO),
            );
            append_usage(
                &mut usage,
                AttributionUsageMetric::BytesIn,
                decimal_detail(&event.details, &["response_bytes"]).unwrap_or(Decimal::ZERO),
            );
            Some((AttributionComponent::Network, usage, Decimal::ZERO))
        }
        EventType::ExternalCost => {
            let quantity = decimal_detail(&event.details, &["attribution_usage_quantity", "units"])
                .unwrap_or(Decimal::ONE);
            let metric = string_detail(&event.details, &["attribution_usage_metric"])
                .and_then(metric_from_str)
                .unwrap_or_else(|| {
                    metric_for_per(
                        string_detail(&event.details, &["attribution_usage_per"])
                            .unwrap_or("request"),
                    )
                });
            append_usage(&mut usage, metric, quantity);
            Some((AttributionComponent::External, usage, Decimal::ZERO))
        }
    }
}

fn provider_for(event: &CostEvent) -> AttributionProviderIdentityV2 {
    let raw_provider = event.provider.as_deref().unwrap_or_default();
    let raw_lower = raw_provider.to_ascii_lowercase();
    let (mut name, mut service) = if raw_lower.contains("openai") {
        ("openai".to_string(), "responses".to_string())
    } else if raw_lower.contains("anthropic") {
        ("anthropic".to_string(), "messages".to_string())
    } else if raw_lower.contains("bedrock") {
        ("aws".to_string(), "bedrock".to_string())
    } else if raw_lower.contains("gemini") || raw_lower == "google" {
        ("google".to_string(), "generate_content".to_string())
    } else if raw_lower.contains("cohere") {
        ("cohere".to_string(), "chat".to_string())
    } else if raw_lower.contains("vercel") {
        ("vercel".to_string(), "ai_sdk".to_string())
    } else if raw_lower.contains("langchain") {
        ("langchain".to_string(), "chat".to_string())
    } else {
        (canonical_name(raw_provider, "unknown"), "api".to_string())
    };

    if event.event_type != EventType::LlmCall {
        let billing = string_detail(&event.details, &["billing_model"]).unwrap_or_default();
        match event.event_type {
            EventType::ComputeCost => {
                name = if billing.starts_with("azure") {
                    "azure".to_string()
                } else if billing == "gce"
                    || billing == "cloud_functions"
                    || billing.starts_with("cloud_")
                {
                    "google_cloud".to_string()
                } else if billing == "vercel_fluid" {
                    "vercel".to_string()
                } else if billing == "k8s_pod" {
                    "kubernetes".to_string()
                } else if matches!(billing, "lambda" | "fargate" | "ec2") {
                    "aws".to_string()
                } else {
                    canonical_name(raw_provider, "runtime")
                };
                service = canonical_name(
                    first_non_empty(&[Some(billing), event.service_name.as_deref()]),
                    "compute",
                );
            }
            EventType::GpuCost => {
                name = canonical_name(
                    first_non_empty(&[
                        string_detail(&event.details, &["cloud_provider"]),
                        event.provider.as_deref(),
                    ]),
                    "runtime",
                );
                service = canonical_name(billing, "gpu");
            }
            EventType::Network => {
                name = canonical_name(
                    first_non_empty(&[
                        string_detail(&event.details, &["cloud_provider"]),
                        event.provider.as_deref(),
                    ]),
                    "internet",
                );
                service = "egress".to_string();
            }
            EventType::ExternalCost => {
                let external_service = event.service_name.as_deref().unwrap_or("external");
                if let Some(tool) = external_service.strip_prefix("mcp:") {
                    name = "mcp".to_string();
                    service = canonical_name(tool, "tool");
                } else if external_service.contains('.') {
                    name = canonical_name(external_service, "external");
                    service = "http_api".to_string();
                } else {
                    name =
                        canonical_name(raw_provider, &canonical_name(external_service, "external"));
                    service = canonical_name(external_service, "api");
                }
            }
            _ => {}
        }
    }

    AttributionProviderIdentityV2 {
        name,
        service,
        record_id: string_detail(
            &event.details,
            &["provider_record_id", "request_id", "call_sid"],
        )
        .filter(|value| value.len() <= 256)
        .map(str::to_string),
        region: string_detail(&event.details, &["region", "cloud_region"])
            .map(|region| canonical_name(region, "unknown")),
    }
}

fn resource_for(event: &CostEvent) -> Option<AttributionResourceV2> {
    if let Some(model) = event.model.as_deref().filter(|value| !value.is_empty()) {
        return Some(AttributionResourceV2 {
            resource_type: AttributionResourceType::Model,
            id: truncate(model, 256),
        });
    }
    match event.event_type {
        EventType::GpuCost => {
            string_detail(&event.details, &["gpu_sku", "instance_type"]).map(|id| {
                AttributionResourceV2 {
                    resource_type: AttributionResourceType::Sku,
                    id: truncate(id, 256),
                }
            })
        }
        EventType::ComputeCost => string_detail(&event.details, &["instance_type", "architecture"])
            .map(|id| AttributionResourceV2 {
                resource_type: AttributionResourceType::Instance,
                id: truncate(id, 256),
            }),
        _ => None,
    }
}

fn cost_evidence_for(event: &CostEvent) -> Option<AttributionCostEvidenceV2> {
    let amount = positive_quantity(event.cost_usd)?;
    let confidence = confidence_for(&event.cost_confidence);
    match event.pricing_source.as_ref()? {
        PricingSource::ProviderResponse => Some(AttributionCostEvidenceV2 {
            amount,
            currency: "USD".to_string(),
            source: AttributionCostEvidenceSource::ProviderReported,
            confidence: if event.cost_confidence == CostConfidence::Exact {
                AttributionCostConfidence::Exact
            } else {
                AttributionCostConfidence::Estimated
            },
            pricing_version: None,
        }),
        PricingSource::Manual | PricingSource::Custom | PricingSource::UserOverride => {
            Some(AttributionCostEvidenceV2 {
                amount,
                currency: "USD".to_string(),
                source: AttributionCostEvidenceSource::Manual,
                confidence,
                pricing_version: None,
            })
        }
        PricingSource::RateRegistry => sdk_evidence(
            event,
            amount,
            AttributionCostEvidenceSource::SdkRateRegistry,
            confidence,
        ),
        PricingSource::ServiceCatalog | PricingSource::Litellm | PricingSource::Tokencost => {
            sdk_evidence(
                event,
                amount,
                AttributionCostEvidenceSource::SdkCatalog,
                confidence,
            )
        }
        PricingSource::Unknown => None,
    }
}

fn sdk_evidence(
    event: &CostEvent,
    amount: String,
    source: AttributionCostEvidenceSource,
    confidence: AttributionCostConfidence,
) -> Option<AttributionCostEvidenceV2> {
    let pricing_version = event
        .pricing_version
        .clone()
        .or_else(|| string_detail(&event.details, &["pricing_version"]).map(str::to_string))?;
    Some(AttributionCostEvidenceV2 {
        amount,
        currency: "USD".to_string(),
        source,
        confidence: if confidence == AttributionCostConfidence::Exact {
            AttributionCostConfidence::Computed
        } else {
            confidence
        },
        pricing_version: Some(pricing_version),
    })
}

fn confidence_for(confidence: &CostConfidence) -> AttributionCostConfidence {
    match confidence {
        CostConfidence::Exact => AttributionCostConfidence::Exact,
        CostConfidence::Computed => AttributionCostConfidence::Computed,
        CostConfidence::Estimated => AttributionCostConfidence::Estimated,
        CostConfidence::Unknown => AttributionCostConfidence::Unknown,
    }
}

fn append_usage(
    usage: &mut Vec<AttributionUsageLineV2>,
    metric: AttributionUsageMetric,
    quantity: Decimal,
) {
    if let Some(quantity) = positive_quantity(quantity) {
        usage.push(AttributionUsageLineV2 {
            metric,
            quantity,
            unit: metric.canonical_unit(),
        });
    }
}

fn positive_quantity(mut value: Decimal) -> Option<String> {
    if value <= Decimal::ZERO {
        return None;
    }
    if value.scale() > 12 {
        value = value.round_dp(12);
    }
    if value <= Decimal::ZERO {
        return None;
    }
    Some(value.normalize().to_string())
}

fn decimal_detail(
    details: &std::collections::HashMap<String, Value>,
    keys: &[&str],
) -> Option<Decimal> {
    for key in keys {
        let Some(value) = details.get(*key) else {
            continue;
        };
        let raw = match value {
            Value::Number(number) => number.to_string(),
            Value::String(value) => value.trim().to_string(),
            _ => continue,
        };
        if let Ok(parsed) = Decimal::from_str(&raw) {
            return Some(parsed);
        }
    }
    None
}

fn string_detail<'a>(
    details: &'a std::collections::HashMap<String, Value>,
    keys: &[&str],
) -> Option<&'a str> {
    keys.iter().find_map(|key| {
        details
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
    })
}

fn metric_from_str(metric: &str) -> Option<AttributionUsageMetric> {
    match metric {
        "input_tokens" => Some(AttributionUsageMetric::InputTokens),
        "output_tokens" => Some(AttributionUsageMetric::OutputTokens),
        "cache_read_input_tokens" => Some(AttributionUsageMetric::CacheReadInputTokens),
        "cache_write_input_tokens" => Some(AttributionUsageMetric::CacheWriteInputTokens),
        "reasoning_output_tokens" => Some(AttributionUsageMetric::ReasoningOutputTokens),
        "characters" => Some(AttributionUsageMetric::Characters),
        "audio_seconds" => Some(AttributionUsageMetric::AudioSeconds),
        "connected_seconds" => Some(AttributionUsageMetric::ConnectedSeconds),
        "recording_seconds" => Some(AttributionUsageMetric::RecordingSeconds),
        "agent_seconds" => Some(AttributionUsageMetric::AgentSeconds),
        "compute_seconds" => Some(AttributionUsageMetric::ComputeSeconds),
        "vcpu_seconds" => Some(AttributionUsageMetric::VcpuSeconds),
        "memory_gib_seconds" => Some(AttributionUsageMetric::MemoryGibSeconds),
        "gpu_seconds" => Some(AttributionUsageMetric::GpuSeconds),
        "request_count" => Some(AttributionUsageMetric::RequestCount),
        "call_count" => Some(AttributionUsageMetric::CallCount),
        "bytes_in" => Some(AttributionUsageMetric::BytesIn),
        "bytes_out" => Some(AttributionUsageMetric::BytesOut),
        "image_count" => Some(AttributionUsageMetric::ImageCount),
        "page_count" => Some(AttributionUsageMetric::PageCount),
        "credit_count" => Some(AttributionUsageMetric::CreditCount),
        _ => None,
    }
}

fn metric_for_per(per: &str) -> AttributionUsageMetric {
    let per = per.to_ascii_lowercase();
    if per.contains("page") {
        AttributionUsageMetric::PageCount
    } else if per.contains("credit") || per.contains("unit") {
        AttributionUsageMetric::CreditCount
    } else if per.contains("image") {
        AttributionUsageMetric::ImageCount
    } else if per.contains("call") || per.contains("sms") || per.contains("message") {
        AttributionUsageMetric::CallCount
    } else if per.contains("character") {
        AttributionUsageMetric::Characters
    } else {
        AttributionUsageMetric::RequestCount
    }
}

fn canonical_name(value: &str, fallback: &str) -> String {
    let lower = value.trim().to_ascii_lowercase();
    let lower = lower
        .strip_prefix("https://")
        .or_else(|| lower.strip_prefix("http://"))
        .unwrap_or(&lower);
    let mut result = String::new();
    let mut replaced = false;
    for ch in lower.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() || matches!(ch, '.' | '_' | '-') {
            result.push(ch);
            replaced = false;
        } else if !replaced {
            result.push('_');
            replaced = true;
        }
        if result.len() >= 128 {
            break;
        }
    }
    let normalized = result.trim_matches(['_', '-', '.']);
    if normalized.is_empty() {
        fallback.to_string()
    } else {
        normalized.to_string()
    }
}

fn first_non_empty<'a>(values: &[Option<&'a str>]) -> &'a str {
    values
        .iter()
        .flatten()
        .copied()
        .find(|value| !value.is_empty())
        .unwrap_or("")
}

fn truncate(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

fn canonical_time(value: chrono::DateTime<chrono::Utc>) -> String {
    value.to_rfc3339_opts(SecondsFormat::Micros, true)
}
