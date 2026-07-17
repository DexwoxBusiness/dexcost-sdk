use std::collections::HashSet;
use std::sync::LazyLock;

use chrono::{DateTime, FixedOffset};
use regex::Regex;
use serde_json::{Map, Value};

use super::types::{AttributionValidationIssue, AttributionValidationResult};

static UUID_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        .expect("valid UUID regex")
});
static CANONICAL_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[a-z0-9][a-z0-9._-]{0,127}$").expect("valid canonical-name regex")
});
static POSITIVE_DECIMAL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:0|[1-9][0-9]{0,25})(?:\.[0-9]{1,12})?$").expect("valid decimal regex")
});
static CURRENCY_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z]{3}$").expect("valid currency regex"));
static TIMESTAMP_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
        .expect("valid timestamp regex")
});

const COMPONENTS: &[&str] = &[
    "llm",
    "telephony",
    "voice_platform",
    "speech_to_text",
    "text_to_speech",
    "realtime_transport",
    "recording",
    "post_call_analysis",
    "compute",
    "gpu",
    "network",
    "storage",
    "external",
];
const RESOURCE_TYPES: &[&str] = &["model", "sku", "instance", "endpoint", "session", "other"];
const LIFECYCLE_STATES: &[&str] = &["pending", "provisional", "final", "voided"];
const EVIDENCE_SOURCES: &[&str] = &[
    "provider_reported",
    "sdk_catalog",
    "sdk_rate_registry",
    "manual",
];
const CONFIDENCES: &[&str] = &["exact", "computed", "estimated", "unknown"];

type ParsedInstant = DateTime<FixedOffset>;

pub fn validate_attribution_event_v2(value: &Value) -> AttributionValidationResult {
    let mut issues = Vec::new();
    let Some(event) = value.as_object() else {
        issue(&mut issues, "", "Event must be an object");
        return result(issues);
    };

    unknown_keys(
        event,
        &[
            "schema_version",
            "event_id",
            "task_id",
            "occurred_at",
            "observed_at",
            "component",
            "provider",
            "resource",
            "lifecycle",
            "usage_period",
            "usage",
            "cost_evidence",
            "retry_of",
        ],
        "",
        &mut issues,
    );

    if event.get("schema_version").and_then(Value::as_str) != Some("2") {
        issue(&mut issues, "schema_version", "Must equal 2");
    }
    valid_string(
        event.get("event_id"),
        "event_id",
        &UUID_PATTERN,
        &mut issues,
    );
    valid_string(event.get("task_id"), "task_id", &UUID_PATTERN, &mut issues);
    parse_timestamp(event.get("occurred_at"), "occurred_at", &mut issues);
    parse_timestamp(event.get("observed_at"), "observed_at", &mut issues);

    match event.get("component").and_then(Value::as_str) {
        Some(component) if COMPONENTS.contains(&component) => {}
        _ => issue(&mut issues, "component", "Unknown attribution component"),
    }
    if let Some(retry_of) = event.get("retry_of") {
        valid_string(Some(retry_of), "retry_of", &UUID_PATTERN, &mut issues);
    }

    validate_provider(event.get("provider"), &mut issues);
    if let Some(resource) = event.get("resource") {
        validate_resource(resource, &mut issues);
    }

    let (state, revision) = validate_lifecycle(event.get("lifecycle"), &mut issues);
    let (period_present, period_closed) =
        validate_usage_period(event.get("usage_period"), &mut issues);
    let (usage_len, has_time_usage) = validate_usage(event.get("usage"), &mut issues);
    let cost = validate_cost_evidence(event.get("cost_evidence"), &mut issues);

    if has_time_usage && matches!(state, Some("provisional" | "final")) && !period_closed {
        issue(
            &mut issues,
            "usage_period.end_at",
            "Finalized time-based usage requires a closed usage period",
        );
    }

    match state {
        Some("pending") => {
            if usage_len != Some(0) {
                issue(&mut issues, "usage", "Pending events cannot assert usage");
            }
            if event.contains_key("cost_evidence") {
                issue(
                    &mut issues,
                    "cost_evidence",
                    "Pending events cannot assert cost",
                );
            }
            if period_present && period_closed {
                issue(
                    &mut issues,
                    "usage_period.end_at",
                    "Pending events cannot close usage",
                );
            }
        }
        Some("provisional") => {
            if usage_len == Some(0) {
                issue(&mut issues, "usage", "Provisional events require usage");
            }
            if cost.confidence == Some("exact") {
                issue(
                    &mut issues,
                    "cost_evidence.confidence",
                    "Provisional cost cannot be exact",
                );
            }
        }
        Some("final") => {
            if usage_len == Some(0) {
                issue(&mut issues, "usage", "Final events require usage");
            }
        }
        Some("voided") => {
            if revision == Some(1) {
                issue(
                    &mut issues,
                    "lifecycle.revision",
                    "Voided events must supersede an earlier revision",
                );
            }
            if usage_len != Some(0) || event.contains_key("cost_evidence") {
                issue(&mut issues, "usage", "Voided events must be tombstones");
            }
        }
        _ => {}
    }

    result(issues)
}

fn validate_provider(value: Option<&Value>, issues: &mut Vec<AttributionValidationIssue>) {
    let Some(provider) = value.and_then(Value::as_object) else {
        issue(issues, "provider", "Provider must be an object");
        return;
    };
    unknown_keys(
        provider,
        &["name", "service", "record_id", "region"],
        "provider",
        issues,
    );
    valid_string(
        provider.get("name"),
        "provider.name",
        &CANONICAL_PATTERN,
        issues,
    );
    valid_string(
        provider.get("service"),
        "provider.service",
        &CANONICAL_PATTERN,
        issues,
    );
    if let Some(record_id) = provider.get("record_id") {
        match record_id.as_str() {
            Some(value) if !value.is_empty() && value.len() <= 256 => {}
            _ => issue(issues, "provider.record_id", "Invalid provider record ID"),
        }
    }
    if let Some(region) = provider.get("region") {
        valid_string(Some(region), "provider.region", &CANONICAL_PATTERN, issues);
    }
}

fn validate_resource(value: &Value, issues: &mut Vec<AttributionValidationIssue>) {
    let Some(resource) = value.as_object() else {
        issue(issues, "resource", "Resource must be an object");
        return;
    };
    unknown_keys(resource, &["type", "id"], "resource", issues);
    match resource.get("type").and_then(Value::as_str) {
        Some(resource_type) if RESOURCE_TYPES.contains(&resource_type) => {}
        _ => issue(issues, "resource.type", "Invalid resource type"),
    }
    match resource.get("id").and_then(Value::as_str) {
        Some(id) if !id.is_empty() && id.len() <= 256 => {}
        _ => issue(issues, "resource.id", "Invalid resource ID"),
    }
}

fn validate_lifecycle<'a>(
    value: Option<&'a Value>,
    issues: &mut Vec<AttributionValidationIssue>,
) -> (Option<&'a str>, Option<u64>) {
    let Some(lifecycle) = value.and_then(Value::as_object) else {
        issue(issues, "lifecycle", "Lifecycle must be an object");
        return (None, None);
    };
    unknown_keys(lifecycle, &["state", "revision"], "lifecycle", issues);
    let state = match lifecycle.get("state").and_then(Value::as_str) {
        Some(state) if LIFECYCLE_STATES.contains(&state) => Some(state),
        _ => {
            issue(issues, "lifecycle.state", "Invalid lifecycle state");
            None
        }
    };
    let revision = match lifecycle.get("revision").and_then(Value::as_u64) {
        Some(revision) if (1..=i32::MAX as u64).contains(&revision) => Some(revision),
        _ => {
            issue(
                issues,
                "lifecycle.revision",
                "Revision must be a positive integer",
            );
            None
        }
    };
    (state, revision)
}

fn validate_usage_period(
    value: Option<&Value>,
    issues: &mut Vec<AttributionValidationIssue>,
) -> (bool, bool) {
    let Some(value) = value else {
        return (false, false);
    };
    let Some(period) = value.as_object() else {
        issue(issues, "usage_period", "Usage period must be an object");
        return (true, false);
    };
    unknown_keys(period, &["start_at", "end_at"], "usage_period", issues);
    let start = parse_timestamp(period.get("start_at"), "usage_period.start_at", issues);
    let end_present = period.contains_key("end_at");
    let end = if end_present {
        parse_timestamp(period.get("end_at"), "usage_period.end_at", issues)
    } else {
        None
    };
    if let (Some(start), Some(end)) = (start, end) {
        if end < start {
            issue(issues, "usage_period.end_at", "End cannot precede start");
        }
    }
    (true, end_present)
}

fn validate_usage(
    value: Option<&Value>,
    issues: &mut Vec<AttributionValidationIssue>,
) -> (Option<usize>, bool) {
    let Some(usage) = value.and_then(Value::as_array) else {
        issue(issues, "usage", "Usage must be an array");
        return (None, false);
    };
    if usage.len() > 32 {
        issue(issues, "usage", "At most 32 usage lines are allowed");
    }
    let mut seen = HashSet::new();
    let mut has_time = false;
    for (index, raw) in usage.iter().enumerate() {
        let prefix = format!("usage.{index}");
        let Some(line) = raw.as_object() else {
            issue(issues, &prefix, "Usage line must be an object");
            continue;
        };
        unknown_keys(line, &["metric", "quantity", "unit"], &prefix, issues);
        let metric = line.get("metric").and_then(Value::as_str);
        let canonical_unit = metric.and_then(canonical_unit_for_metric);
        match (metric, canonical_unit) {
            (Some(metric), Some(_)) if !seen.insert(metric) => issue(
                issues,
                &format!("{prefix}.metric"),
                "Duplicate usage metric",
            ),
            (Some(_), Some(_)) => {}
            _ => issue(issues, &format!("{prefix}.metric"), "Invalid usage metric"),
        }
        match line.get("quantity").and_then(Value::as_str) {
            Some(quantity)
                if POSITIVE_DECIMAL.is_match(quantity)
                    && quantity.bytes().any(|byte| matches!(byte, b'1'..=b'9')) => {}
            _ => issue(
                issues,
                &format!("{prefix}.quantity"),
                "Must be a positive plain decimal string",
            ),
        }
        let unit = line.get("unit").and_then(Value::as_str);
        if !matches!(
            unit,
            Some(
                "Tokens"
                    | "Characters"
                    | "Seconds"
                    | "vCPU-Seconds"
                    | "GiB-Seconds"
                    | "GPU-Seconds"
                    | "Requests"
                    | "Calls"
                    | "Bytes"
                    | "Images"
                    | "Pages"
                    | "Credits"
            )
        ) {
            issue(issues, &format!("{prefix}.unit"), "Invalid usage unit");
        } else {
            has_time |= unit.is_some_and(|unit| unit.ends_with("Seconds"));
            if let (Some(canonical), Some(actual)) = (canonical_unit, unit) {
                if actual != canonical {
                    issue(
                        issues,
                        &format!("{prefix}.unit"),
                        "Metric must use its canonical unit",
                    );
                }
            }
        }
    }
    (Some(usage.len()), has_time)
}

#[derive(Default)]
struct ParsedCostEvidence<'a> {
    confidence: Option<&'a str>,
}

fn validate_cost_evidence<'a>(
    value: Option<&'a Value>,
    issues: &mut Vec<AttributionValidationIssue>,
) -> ParsedCostEvidence<'a> {
    let Some(value) = value else {
        return ParsedCostEvidence::default();
    };
    let Some(cost) = value.as_object() else {
        issue(issues, "cost_evidence", "Cost evidence must be an object");
        return ParsedCostEvidence::default();
    };
    unknown_keys(
        cost,
        &[
            "amount",
            "currency",
            "source",
            "confidence",
            "pricing_version",
        ],
        "cost_evidence",
        issues,
    );
    match cost.get("amount").and_then(Value::as_str) {
        Some(amount)
            if POSITIVE_DECIMAL.is_match(amount)
                && amount.bytes().any(|byte| matches!(byte, b'1'..=b'9')) => {}
        _ => issue(
            issues,
            "cost_evidence.amount",
            "Must be a positive plain decimal string",
        ),
    }
    match cost.get("currency").and_then(Value::as_str) {
        Some(currency) if CURRENCY_PATTERN.is_match(currency) => {}
        _ => issue(issues, "cost_evidence.currency", "Invalid currency"),
    }
    let source = match cost.get("source").and_then(Value::as_str) {
        Some(source) if EVIDENCE_SOURCES.contains(&source) => Some(source),
        _ => {
            issue(issues, "cost_evidence.source", "Invalid evidence source");
            None
        }
    };
    let confidence = match cost.get("confidence").and_then(Value::as_str) {
        Some(confidence) if CONFIDENCES.contains(&confidence) => Some(confidence),
        _ => {
            issue(issues, "cost_evidence.confidence", "Invalid confidence");
            None
        }
    };
    if let Some(version) = cost.get("pricing_version") {
        match version.as_str() {
            Some(version) if !version.is_empty() && version.len() <= 128 => {}
            _ => issue(
                issues,
                "cost_evidence.pricing_version",
                "Invalid pricing version",
            ),
        }
    }
    if source == Some("provider_reported")
        && confidence.is_some()
        && !matches!(confidence, Some("exact" | "estimated"))
    {
        issue(
            issues,
            "cost_evidence.confidence",
            "Provider-reported cost must be exact or estimated",
        );
    }
    if matches!(source, Some("sdk_catalog" | "sdk_rate_registry")) {
        if confidence == Some("exact") {
            issue(
                issues,
                "cost_evidence.confidence",
                "SDK-derived cost cannot be exact",
            );
        }
        if !cost.contains_key("pricing_version") {
            issue(
                issues,
                "cost_evidence.pricing_version",
                "SDK-derived cost requires pricing_version",
            );
        }
    }
    ParsedCostEvidence { confidence }
}

fn canonical_unit_for_metric(metric: &str) -> Option<&'static str> {
    match metric {
        "input_tokens"
        | "output_tokens"
        | "cache_read_input_tokens"
        | "cache_write_input_tokens"
        | "reasoning_output_tokens" => Some("Tokens"),
        "characters" => Some("Characters"),
        "audio_seconds" | "connected_seconds" | "recording_seconds" | "agent_seconds"
        | "compute_seconds" => Some("Seconds"),
        "vcpu_seconds" => Some("vCPU-Seconds"),
        "memory_gib_seconds" => Some("GiB-Seconds"),
        "gpu_seconds" => Some("GPU-Seconds"),
        "request_count" => Some("Requests"),
        "call_count" => Some("Calls"),
        "bytes_in" | "bytes_out" => Some("Bytes"),
        "image_count" => Some("Images"),
        "page_count" => Some("Pages"),
        "credit_count" => Some("Credits"),
        _ => None,
    }
}

fn parse_timestamp(
    value: Option<&Value>,
    path: &str,
    issues: &mut Vec<AttributionValidationIssue>,
) -> Option<ParsedInstant> {
    let Some(raw) = value.and_then(Value::as_str) else {
        issue(issues, path, "Invalid string value");
        return None;
    };
    if !TIMESTAMP_PATTERN.is_match(raw) {
        issue(issues, path, "Invalid string value");
        return None;
    }
    match DateTime::parse_from_rfc3339(raw) {
        Ok(parsed) => Some(parsed),
        Err(_) => {
            issue(
                issues,
                path,
                "Timestamp must be a valid ISO 8601 calendar instant",
            );
            None
        }
    }
}

fn valid_string(
    value: Option<&Value>,
    path: &str,
    pattern: &Regex,
    issues: &mut Vec<AttributionValidationIssue>,
) -> bool {
    match value.and_then(Value::as_str) {
        Some(value) if !value.is_empty() && pattern.is_match(value) => true,
        _ => {
            issue(issues, path, "Invalid string value");
            false
        }
    }
}

fn unknown_keys(
    object: &Map<String, Value>,
    allowed: &[&str],
    prefix: &str,
    issues: &mut Vec<AttributionValidationIssue>,
) {
    for key in object.keys() {
        if !allowed.contains(&key.as_str()) {
            let path = if prefix.is_empty() {
                key.clone()
            } else {
                format!("{prefix}.{key}")
            };
            issue(issues, &path, "Unknown field");
        }
    }
}

fn issue(issues: &mut Vec<AttributionValidationIssue>, path: &str, message: &str) {
    issues.push(AttributionValidationIssue {
        path: path.to_string(),
        message: message.to_string(),
    });
}

fn result(issues: Vec<AttributionValidationIssue>) -> AttributionValidationResult {
    AttributionValidationResult {
        success: issues.is_empty(),
        issues,
    }
}
