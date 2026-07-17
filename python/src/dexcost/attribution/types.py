"""Public attribution-v2 wire contract types and canonical value sets."""

from __future__ import annotations

from typing import Literal, TypedDict

ATTRIBUTION_V2_CONTRACT_VERSION = "2.0.0"

ATTRIBUTION_COMPONENTS = (
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
)

ATTRIBUTION_USAGE_METRICS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "characters",
    "audio_seconds",
    "connected_seconds",
    "recording_seconds",
    "agent_seconds",
    "compute_seconds",
    "vcpu_seconds",
    "memory_gib_seconds",
    "gpu_seconds",
    "request_count",
    "call_count",
    "bytes_in",
    "bytes_out",
    "image_count",
    "page_count",
    "credit_count",
)

ATTRIBUTION_USAGE_UNITS = (
    "Tokens",
    "Characters",
    "Seconds",
    "vCPU-Seconds",
    "GiB-Seconds",
    "GPU-Seconds",
    "Requests",
    "Calls",
    "Bytes",
    "Images",
    "Pages",
    "Credits",
)

AttributionComponent = Literal[
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
]
AttributionUsageMetric = Literal[
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "characters",
    "audio_seconds",
    "connected_seconds",
    "recording_seconds",
    "agent_seconds",
    "compute_seconds",
    "vcpu_seconds",
    "memory_gib_seconds",
    "gpu_seconds",
    "request_count",
    "call_count",
    "bytes_in",
    "bytes_out",
    "image_count",
    "page_count",
    "credit_count",
]
AttributionUsageUnit = Literal[
    "Tokens",
    "Characters",
    "Seconds",
    "vCPU-Seconds",
    "GiB-Seconds",
    "GPU-Seconds",
    "Requests",
    "Calls",
    "Bytes",
    "Images",
    "Pages",
    "Credits",
]
AttributionConfidence = Literal["exact", "computed", "estimated", "unknown"]
AttributionLifecycleState = Literal["pending", "provisional", "final", "voided"]
AttributionCostEvidenceSource = Literal[
    "provider_reported", "sdk_catalog", "sdk_rate_registry", "manual"
]

ATTRIBUTION_UNIT_BY_METRIC: dict[AttributionUsageMetric, AttributionUsageUnit] = {
    "input_tokens": "Tokens",
    "output_tokens": "Tokens",
    "cache_read_input_tokens": "Tokens",
    "cache_write_input_tokens": "Tokens",
    "reasoning_output_tokens": "Tokens",
    "characters": "Characters",
    "audio_seconds": "Seconds",
    "connected_seconds": "Seconds",
    "recording_seconds": "Seconds",
    "agent_seconds": "Seconds",
    "compute_seconds": "Seconds",
    "vcpu_seconds": "vCPU-Seconds",
    "memory_gib_seconds": "GiB-Seconds",
    "gpu_seconds": "GPU-Seconds",
    "request_count": "Requests",
    "call_count": "Calls",
    "bytes_in": "Bytes",
    "bytes_out": "Bytes",
    "image_count": "Images",
    "page_count": "Pages",
    "credit_count": "Credits",
}


class AttributionUsageLineV2(TypedDict):
    metric: AttributionUsageMetric
    quantity: str
    unit: AttributionUsageUnit


class _AttributionProviderIdentityV2Required(TypedDict):
    name: str
    service: str


class AttributionProviderIdentityV2(_AttributionProviderIdentityV2Required, total=False):
    record_id: str
    region: str


class AttributionResourceV2(TypedDict):
    type: Literal["model", "sku", "instance", "endpoint", "session", "other"]
    id: str


class _AttributionCostEvidenceV2Required(TypedDict):
    amount: str
    currency: str
    source: AttributionCostEvidenceSource
    confidence: AttributionConfidence


class AttributionCostEvidenceV2(_AttributionCostEvidenceV2Required, total=False):
    pricing_version: str


class AttributionLifecycleV2(TypedDict):
    state: AttributionLifecycleState
    revision: int


class _AttributionUsagePeriodV2Required(TypedDict):
    start_at: str


class AttributionUsagePeriodV2(_AttributionUsagePeriodV2Required, total=False):
    end_at: str


class _AttributionEventV2Required(TypedDict):
    schema_version: Literal["2"]
    event_id: str
    task_id: str
    occurred_at: str
    observed_at: str
    component: AttributionComponent
    provider: AttributionProviderIdentityV2
    lifecycle: AttributionLifecycleV2
    usage: list[AttributionUsageLineV2]


class AttributionEventV2(_AttributionEventV2Required, total=False):
    resource: AttributionResourceV2
    usage_period: AttributionUsagePeriodV2
    cost_evidence: AttributionCostEvidenceV2
    retry_of: str


class AttributionTaskIngestV1(TypedDict):
    task_id: str
    task_type: str
    status: Literal["pending", "running", "success", "failed"]
    started_at: str
    ended_at: str | None
    metadata: dict[str, object]
    customer_id: str | None
    project_id: str | None
    parent_task_id: str | None
    experiment_id: str | None
    variant: str | None
    schema_version: Literal["1"]
