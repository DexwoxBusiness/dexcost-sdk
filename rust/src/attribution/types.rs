use std::collections::HashMap;

use serde::{Deserialize, Serialize};

pub const CONTRACT_VERSION: &str = "2.0.0";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionComponent {
    Llm,
    Telephony,
    VoicePlatform,
    SpeechToText,
    TextToSpeech,
    RealtimeTransport,
    Recording,
    PostCallAnalysis,
    Compute,
    Gpu,
    Network,
    Storage,
    External,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionUsageMetric {
    InputTokens,
    OutputTokens,
    CacheReadInputTokens,
    CacheWriteInputTokens,
    ReasoningOutputTokens,
    Characters,
    AudioSeconds,
    ConnectedSeconds,
    RecordingSeconds,
    AgentSeconds,
    ComputeSeconds,
    VcpuSeconds,
    MemoryGibSeconds,
    GpuSeconds,
    RequestCount,
    CallCount,
    BytesIn,
    BytesOut,
    ImageCount,
    PageCount,
    CreditCount,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AttributionUsageUnit {
    #[serde(rename = "Tokens")]
    Tokens,
    #[serde(rename = "Characters")]
    Characters,
    #[serde(rename = "Seconds")]
    Seconds,
    #[serde(rename = "vCPU-Seconds")]
    VcpuSeconds,
    #[serde(rename = "GiB-Seconds")]
    GibSeconds,
    #[serde(rename = "GPU-Seconds")]
    GpuSeconds,
    #[serde(rename = "Requests")]
    Requests,
    #[serde(rename = "Calls")]
    Calls,
    #[serde(rename = "Bytes")]
    Bytes,
    #[serde(rename = "Images")]
    Images,
    #[serde(rename = "Pages")]
    Pages,
    #[serde(rename = "Credits")]
    Credits,
}

impl AttributionUsageMetric {
    pub fn canonical_unit(self) -> AttributionUsageUnit {
        match self {
            Self::InputTokens
            | Self::OutputTokens
            | Self::CacheReadInputTokens
            | Self::CacheWriteInputTokens
            | Self::ReasoningOutputTokens => AttributionUsageUnit::Tokens,
            Self::Characters => AttributionUsageUnit::Characters,
            Self::AudioSeconds
            | Self::ConnectedSeconds
            | Self::RecordingSeconds
            | Self::AgentSeconds
            | Self::ComputeSeconds => AttributionUsageUnit::Seconds,
            Self::VcpuSeconds => AttributionUsageUnit::VcpuSeconds,
            Self::MemoryGibSeconds => AttributionUsageUnit::GibSeconds,
            Self::GpuSeconds => AttributionUsageUnit::GpuSeconds,
            Self::RequestCount => AttributionUsageUnit::Requests,
            Self::CallCount => AttributionUsageUnit::Calls,
            Self::BytesIn | Self::BytesOut => AttributionUsageUnit::Bytes,
            Self::ImageCount => AttributionUsageUnit::Images,
            Self::PageCount => AttributionUsageUnit::Pages,
            Self::CreditCount => AttributionUsageUnit::Credits,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionUsageLineV2 {
    pub metric: AttributionUsageMetric,
    pub quantity: String,
    pub unit: AttributionUsageUnit,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionProviderIdentityV2 {
    pub name: String,
    pub service: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub record_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub region: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionResourceType {
    Model,
    Sku,
    Instance,
    Endpoint,
    Session,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionResourceV2 {
    #[serde(rename = "type")]
    pub resource_type: AttributionResourceType,
    pub id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionCostEvidenceSource {
    ProviderReported,
    SdkCatalog,
    SdkRateRegistry,
    Manual,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionCostConfidence {
    Exact,
    Computed,
    Estimated,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionCostEvidenceV2 {
    pub amount: String,
    pub currency: String,
    pub source: AttributionCostEvidenceSource,
    pub confidence: AttributionCostConfidence,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pricing_version: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttributionLifecycleState {
    Pending,
    Provisional,
    Final,
    Voided,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionLifecycleV2 {
    pub state: AttributionLifecycleState,
    pub revision: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionUsagePeriodV2 {
    pub start_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionEventV2 {
    pub schema_version: String,
    pub event_id: String,
    pub task_id: String,
    pub occurred_at: String,
    pub observed_at: String,
    pub component: AttributionComponent,
    pub provider: AttributionProviderIdentityV2,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resource: Option<AttributionResourceV2>,
    pub lifecycle: AttributionLifecycleV2,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage_period: Option<AttributionUsagePeriodV2>,
    pub usage: Vec<AttributionUsageLineV2>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost_evidence: Option<AttributionCostEvidenceV2>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_of: Option<String>,
}

/// Task ingestion intentionally excludes aggregate costs and token totals.
/// The control plane derives those values from revisioned attribution lines.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttributionTaskIngestV1 {
    pub task_id: String,
    pub task_type: String,
    pub status: String,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub metadata: HashMap<String, serde_json::Value>,
    pub customer_id: Option<String>,
    pub project_id: Option<String>,
    pub parent_task_id: Option<String>,
    pub experiment_id: Option<String>,
    pub variant: Option<String>,
    pub schema_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AttributionValidationIssue {
    pub path: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AttributionValidationResult {
    pub success: bool,
    pub issues: Vec<AttributionValidationIssue>,
}
