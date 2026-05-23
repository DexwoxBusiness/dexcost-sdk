use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use uuid::Uuid;

/// TaskStatus represents the lifecycle status of a tracked task.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Running,
    Pending,
    Success,
    Failed,
}

/// EventType discriminates cost-generating events.
///
/// Phase 2 adds GPU-specific event types:
/// - `GpuCost`: per-task aggregated GPU cost event (one per task)
/// - `GpuUtilizationSignal`: per-device observability event with no
///   cost_usd / pricing_source / pricing_version. The Control Layer
///   MUST NOT aggregate signal events into any dollar field
///   (convention §1 carve-out, Decision #3).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EventType {
    LlmCall,
    ExternalCost,
    ComputeCost,
    RetryMarker,
    Network,
    GpuCost,
    GpuUtilizationSignal,
}

/// CostConfidence indicates how trustworthy the reported cost is.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CostConfidence {
    Exact,
    Computed,
    Estimated,
    Unknown,
}

/// PricingSource indicates where the cost figure was derived from.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PricingSource {
    Litellm,
    Tokencost,
    ProviderResponse,
    Manual,
    Custom,
    RateRegistry,
    ServiceCatalog,
    UserOverride,
    Unknown,
}

/// Task represents a tracked business task (e.g. "resolve support ticket").
/// All downstream events roll up into the aggregated cost and token fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub task_id: String,
    pub task_type: String,
    pub status: TaskStatus,
    pub started_at: DateTime<Utc>,
    pub ended_at: Option<DateTime<Utc>>,
    pub metadata: HashMap<String, serde_json::Value>,
    pub customer_id: Option<String>,
    pub project_id: Option<String>,
    pub parent_task_id: Option<String>,
    pub experiment_id: Option<String>,
    pub variant: Option<String>,
    // Aggregated costs (Decimal for precision)
    pub llm_cost_usd: Decimal,
    pub external_cost_usd: Decimal,
    pub compute_cost_usd: Decimal,
    pub total_cost_usd: Decimal,
    pub total_input_tokens: i64,
    pub total_output_tokens: i64,
    pub total_cached_tokens: i64,
    pub retry_count: i32,
    pub retry_cost_usd: Decimal,
    pub failure_count: i32,
    // Network capture v1 — bytes-only counters + per-host breakdown.
    // `network_by_host` is a free-form JSON blob (matches existing pattern)
    // and defaults to `{"hosts": []}` so legacy payloads round-trip cleanly.
    #[serde(default)]
    pub network_bytes_in: i64,
    #[serde(default)]
    pub network_bytes_out: i64,
    #[serde(default)]
    pub network_call_count: i64,
    #[serde(default = "default_network_by_host")]
    pub network_by_host: serde_json::Value,
    /// v2 — cloud-egress cost in USD, computed at task finalize from the
    /// accountant's canonical external_bytes_out scalar. Distinct from
    /// `external_cost_usd` (vendor API charges) — see Decision #7. Defaults
    /// to zero for legacy payloads.
    #[serde(default)]
    pub network_cost_usd: Decimal,
    /// Phase 2 GPU — aggregated GPU cost in USD, computed at task finalize
    /// by `_finalize_gpu`. Distinct from `compute_cost_usd` (CPU/memory) and
    /// `external_cost_usd` (vendor API charges). `total_cost_usd` =
    /// `llm + external + compute + network + gpu`. Defaults to zero for
    /// legacy payloads.
    #[serde(default)]
    pub gpu_cost_usd: Decimal,
    /// In-memory accumulator for HTTP byte usage. Not serialised — each
    /// deserialised Task gets a fresh accountant. Cloning the Task shares
    /// the accountant by Arc-refcount, which is the right behaviour during
    /// storage I/O (no records happen against stored clones) and matches
    /// Python's `_network` field semantically.
    #[serde(skip, default)]
    pub network_accountant: std::sync::Arc<crate::adapters::network_accountant::NetworkAccountant>,
    /// Optional in-memory compute accountant. Not serialised — set by the
    /// handler wraps / auto-task path. Mirrors Python's `_compute` field.
    #[serde(skip, default)]
    pub compute: Option<std::sync::Arc<crate::core::compute_accountant::ComputeAccountant>>,
    /// Optional in-memory GPU accountant. Not serialised — set by the
    /// GPU handler wraps / auto-task path. Mirrors Python's `_gpu` field.
    /// Set lazily by Modal / RunPod / Replicate wraps OR by the tracker
    /// when a long-running GPU runtime is detected.
    #[serde(skip, default)]
    pub gpu: Option<std::sync::Arc<crate::core::gpu_accountant::GpuAccountant>>,
    pub schema_version: String,
}

fn default_network_by_host() -> serde_json::Value {
    serde_json::json!({"hosts": []})
}

impl Task {
    /// Creates a new Task with sensible defaults and a new UUID.
    pub fn new(task_type: &str) -> Self {
        Self {
            task_id: Uuid::new_v4().to_string(),
            task_type: task_type.to_string(),
            status: TaskStatus::Pending,
            started_at: Utc::now(),
            ended_at: None,
            metadata: HashMap::new(),
            customer_id: None,
            project_id: None,
            parent_task_id: None,
            experiment_id: None,
            variant: None,
            llm_cost_usd: Decimal::ZERO,
            external_cost_usd: Decimal::ZERO,
            compute_cost_usd: Decimal::ZERO,
            total_cost_usd: Decimal::ZERO,
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_cached_tokens: 0,
            retry_count: 0,
            retry_cost_usd: Decimal::ZERO,
            failure_count: 0,
            network_bytes_in: 0,
            network_bytes_out: 0,
            network_call_count: 0,
            network_by_host: default_network_by_host(),
            network_cost_usd: Decimal::ZERO,
            gpu_cost_usd: Decimal::ZERO,
            network_accountant: std::sync::Arc::default(),
            compute: None,
            gpu: None,
            schema_version: "1".to_string(),
        }
    }

    /// Serializes the Task to a map matching the Standard Event Schema v1 wire
    /// format. Costs are serialized as strings to preserve precision.
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "started_at": self.started_at.to_rfc3339_opts(chrono::SecondsFormat::Nanos, true),
            "ended_at": self.ended_at.map(|t| t.to_rfc3339_opts(chrono::SecondsFormat::Nanos, true)),
            "metadata": self.metadata,
            "customer_id": self.customer_id,
            "project_id": self.project_id,
            "parent_task_id": self.parent_task_id,
            "experiment_id": self.experiment_id,
            "variant": self.variant,
            "llm_cost_usd": self.llm_cost_usd.to_string(),
            "external_cost_usd": self.external_cost_usd.to_string(),
            "compute_cost_usd": self.compute_cost_usd.to_string(),
            "total_cost_usd": self.total_cost_usd.to_string(),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "retry_count": self.retry_count,
            "retry_cost_usd": self.retry_cost_usd.to_string(),
            "failure_count": self.failure_count,
            "network_bytes_in": self.network_bytes_in,
            "network_bytes_out": self.network_bytes_out,
            "network_call_count": self.network_call_count,
            "network_by_host": self.network_by_host,
            "network_cost_usd": self.network_cost_usd.to_string(),
            "gpu_cost_usd": self.gpu_cost_usd.to_string(),
            "schema_version": self.schema_version,
        })
    }
}

/// CostEvent represents a single cost event (LLM call, external API, compute, retry).
/// Matches the Dexcost Standard Event Schema v1.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CostEvent {
    pub event_id: String,
    pub task_id: String,
    pub event_type: EventType,
    pub occurred_at: DateTime<Utc>,
    pub cost_usd: Decimal,
    pub cost_confidence: CostConfidence,
    pub pricing_source: Option<PricingSource>,
    pub pricing_version: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cached_tokens: Option<i64>,
    pub latency_ms: Option<i64>,
    pub service_name: Option<String>,
    pub is_retry: bool,
    pub retry_reason: Option<String>,
    pub retry_of: Option<String>,
    pub details: HashMap<String, serde_json::Value>,
    pub schema_version: String,
}

impl CostEvent {
    /// Creates a new CostEvent with sensible defaults and a new UUID.
    pub fn new(task_id: &str, event_type: EventType) -> Self {
        Self {
            event_id: Uuid::new_v4().to_string(),
            task_id: task_id.to_string(),
            event_type,
            occurred_at: Utc::now(),
            cost_usd: Decimal::ZERO,
            cost_confidence: CostConfidence::Exact,
            pricing_source: None,
            pricing_version: None,
            provider: None,
            model: None,
            input_tokens: None,
            output_tokens: None,
            cached_tokens: None,
            latency_ms: None,
            service_name: None,
            is_retry: false,
            retry_reason: None,
            retry_of: None,
            details: HashMap::new(),
            schema_version: "1".to_string(),
        }
    }

    /// Serializes the CostEvent to a map matching the Standard Event Schema v1
    /// wire format. Costs are serialized as strings to preserve precision.
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.to_rfc3339_opts(chrono::SecondsFormat::Nanos, true),
            "cost_usd": self.cost_usd.to_string(),
            "cost_confidence": self.cost_confidence,
            "pricing_source": self.pricing_source,
            "pricing_version": self.pricing_version,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "latency_ms": self.latency_ms,
            "service_name": self.service_name,
            "is_retry": self.is_retry,
            "retry_reason": self.retry_reason,
            "retry_of": self.retry_of,
            "details": self.details,
            "schema_version": self.schema_version,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_task_new_defaults() {
        let task = Task::new("resolve_ticket");
        assert_eq!(task.task_type, "resolve_ticket");
        assert_eq!(task.status, TaskStatus::Pending);
        assert_eq!(task.llm_cost_usd, Decimal::ZERO);
        assert_eq!(task.schema_version, "1");
        assert!(task.ended_at.is_none());
        assert!(!task.task_id.is_empty());
    }

    #[test]
    fn test_event_new_defaults() {
        let event = CostEvent::new("task-123", EventType::LlmCall);
        assert_eq!(event.task_id, "task-123");
        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.cost_usd, Decimal::ZERO);
        assert_eq!(event.cost_confidence, CostConfidence::Exact);
        assert!(!event.is_retry);
        assert_eq!(event.schema_version, "1");
    }

    #[test]
    fn test_task_to_dict_costs_as_strings() {
        let task = Task::new("test");
        let dict = task.to_dict();
        // Costs must be strings in JSON output
        assert!(dict["llm_cost_usd"].is_string());
        assert!(dict["external_cost_usd"].is_string());
        assert!(dict["compute_cost_usd"].is_string());
        assert!(dict["total_cost_usd"].is_string());
        assert!(dict["retry_cost_usd"].is_string());
    }

    #[test]
    fn test_event_to_dict_cost_as_string() {
        let event = CostEvent::new("task-123", EventType::ExternalCost);
        let dict = event.to_dict();
        assert!(dict["cost_usd"].is_string());
    }

    #[test]
    fn test_task_status_serialization() {
        let val = serde_json::to_string(&TaskStatus::Success).unwrap();
        assert_eq!(val, "\"success\"");
        let val = serde_json::to_string(&TaskStatus::Failed).unwrap();
        assert_eq!(val, "\"failed\"");
        let val = serde_json::to_string(&TaskStatus::Pending).unwrap();
        assert_eq!(val, "\"pending\"");
    }

    #[test]
    fn test_event_type_serialization() {
        let val = serde_json::to_string(&EventType::LlmCall).unwrap();
        assert_eq!(val, "\"llm_call\"");
        let val = serde_json::to_string(&EventType::RetryMarker).unwrap();
        assert_eq!(val, "\"retry_marker\"");
    }

    #[test]
    fn test_event_type_network_serializes_to_network() {
        let val = serde_json::to_string(&EventType::Network).unwrap();
        assert_eq!(val, "\"network\"");
    }

    #[test]
    fn test_event_type_network_round_trip() {
        let parsed: EventType = serde_json::from_str("\"network\"").unwrap();
        assert_eq!(parsed, EventType::Network);
    }

    #[test]
    fn test_task_network_field_defaults() {
        let task = Task::new("x");
        assert_eq!(task.network_bytes_in, 0);
        assert_eq!(task.network_bytes_out, 0);
        assert_eq!(task.network_call_count, 0);
        assert_eq!(task.network_by_host, serde_json::json!({"hosts": []}));
    }

    #[test]
    fn test_task_network_fields_in_to_dict() {
        let task = Task::new("x");
        let dict = task.to_dict();
        assert_eq!(dict["network_bytes_in"], serde_json::json!(0));
        assert_eq!(dict["network_bytes_out"], serde_json::json!(0));
        assert_eq!(dict["network_call_count"], serde_json::json!(0));
        assert_eq!(dict["network_by_host"], serde_json::json!({"hosts": []}));
    }

    #[test]
    fn test_task_network_fields_round_trip() {
        let mut task = Task::new("x");
        task.network_bytes_in = 4096;
        task.network_bytes_out = 512;
        task.network_call_count = 3;
        task.network_by_host = serde_json::json!({
            "hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]
        });
        let value = serde_json::to_value(&task).unwrap();
        let restored: Task = serde_json::from_value(value).unwrap();
        assert_eq!(restored.network_bytes_in, 4096);
        assert_eq!(restored.network_bytes_out, 512);
        assert_eq!(restored.network_call_count, 3);
        assert_eq!(
            restored.network_by_host,
            serde_json::json!({
                "hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]
            })
        );
    }

    // ── Phase 2 GPU foundation: gpu_cost_usd + new EventType variants ──

    #[test]
    fn test_event_type_gpu_cost_serializes() {
        let val = serde_json::to_string(&EventType::GpuCost).unwrap();
        assert_eq!(val, "\"gpu_cost\"");
    }

    #[test]
    fn test_event_type_gpu_utilization_signal_serializes() {
        let val = serde_json::to_string(&EventType::GpuUtilizationSignal).unwrap();
        assert_eq!(val, "\"gpu_utilization_signal\"");
    }

    #[test]
    fn test_event_type_gpu_round_trip() {
        let parsed: EventType = serde_json::from_str("\"gpu_cost\"").unwrap();
        assert_eq!(parsed, EventType::GpuCost);
        let parsed: EventType = serde_json::from_str("\"gpu_utilization_signal\"").unwrap();
        assert_eq!(parsed, EventType::GpuUtilizationSignal);
    }

    #[test]
    fn test_task_gpu_cost_usd_defaults_to_zero() {
        let task = Task::new("x");
        assert_eq!(task.gpu_cost_usd, Decimal::ZERO);
    }

    #[test]
    fn test_task_gpu_cost_usd_in_to_dict() {
        let task = Task::new("x");
        let dict = task.to_dict();
        assert_eq!(dict["gpu_cost_usd"], serde_json::json!("0"));
        assert!(dict["gpu_cost_usd"].is_string());
    }

    #[test]
    fn test_task_gpu_cost_usd_round_trip() {
        let mut task = Task::new("x");
        task.gpu_cost_usd = Decimal::new(12345, 4); // 1.2345
        let value = serde_json::to_value(&task).unwrap();
        let restored: Task = serde_json::from_value(value).unwrap();
        assert_eq!(restored.gpu_cost_usd, Decimal::new(12345, 4));
    }

    #[test]
    fn test_task_gpu_cost_usd_absent_in_payload_defaults_to_zero() {
        // Legacy payloads without gpu_cost_usd round-trip with zero default.
        let mut value = serde_json::to_value(Task::new("x")).unwrap();
        value.as_object_mut().unwrap().remove("gpu_cost_usd");
        let restored: Task = serde_json::from_value(value).unwrap();
        assert_eq!(restored.gpu_cost_usd, Decimal::ZERO);
    }

    #[test]
    fn test_task_network_fields_absent_in_payload_default() {
        // Legacy payloads without the four network_* keys round-trip with defaults.
        let mut value = serde_json::to_value(Task::new("x")).unwrap();
        let obj = value.as_object_mut().unwrap();
        obj.remove("network_bytes_in");
        obj.remove("network_bytes_out");
        obj.remove("network_call_count");
        obj.remove("network_by_host");
        let restored: Task = serde_json::from_value(value).unwrap();
        assert_eq!(restored.network_bytes_in, 0);
        assert_eq!(restored.network_bytes_out, 0);
        assert_eq!(restored.network_call_count, 0);
        assert_eq!(restored.network_by_host, serde_json::json!({"hosts": []}));
    }
}
