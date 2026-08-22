"""Public attribution-v3 observation contract types."""

from __future__ import annotations

from typing import Literal, TypedDict

from dexcost.attribution.types import (
    AttributionComponent,
    AttributionConfidence,
    AttributionCostEvidenceSource,
    AttributionLifecycleState,
)

ATTRIBUTION_V3_CONTRACT_VERSION = "3.2.0"

AttributionUsageMetricV3 = str
AttributionUsageUnitV3 = str
AttributionOperationStatusV3 = Literal[
    "in_progress", "succeeded", "failed", "cancelled", "unknown"
]


class AttributionStringDimensionValue(TypedDict):
    type: Literal["string"]
    value: str


class AttributionBooleanDimensionValue(TypedDict):
    type: Literal["boolean"]
    value: bool


class AttributionIntegerDimensionValue(TypedDict):
    type: Literal["integer"]
    value: str


class AttributionDecimalDimensionValue(TypedDict):
    type: Literal["decimal"]
    value: str


AttributionBillingDimensionValue = (
    AttributionStringDimensionValue
    | AttributionBooleanDimensionValue
    | AttributionIntegerDimensionValue
    | AttributionDecimalDimensionValue
)


class AttributionBillingDimension(TypedDict):
    key: str
    value: AttributionBillingDimensionValue


class AttributionUsageLineV3(TypedDict):
    line_id: str
    metric: AttributionUsageMetricV3
    quantity: str
    unit: AttributionUsageUnitV3
    dimensions: list[AttributionBillingDimension]


class _AttributionProviderIdentityV3Required(TypedDict):
    name: str
    service: str


class AttributionProviderIdentityV3(_AttributionProviderIdentityV3Required, total=False):
    record_id: str
    region: str


AttributionResourceTypeV3 = Literal[
    "model", "sku", "instance", "endpoint", "session", "other", "tool"
]


class AttributionResourceV3(TypedDict):
    """Resource identity. ``"tool"`` was added by the in-place v3 extension."""

    type: AttributionResourceTypeV3
    id: str


AttributionCapabilityKindV3 = Literal["tool", "skill", "workflow", "extension", "other"]
AttributionCapabilitySourceV3 = Literal[
    "built_in", "project", "user", "plugin", "marketplace", "remote", "other"
]
AttributionCapabilityInvocationV3 = Literal[
    "explicit", "automatic", "nested", "scheduled", "remote", "other"
]


class _AttributionCapabilityIdentityV3Required(TypedDict):
    name: str
    kind: AttributionCapabilityKindV3


class AttributionCapabilityIdentityV3(
    _AttributionCapabilityIdentityV3Required, total=False
):
    namespace: str
    version: str
    source: AttributionCapabilitySourceV3
    source_id: str
    invocation: AttributionCapabilityInvocationV3


class AttributionTraceIdentityV3(TypedDict):
    trace_id: str
    span_id: str


class _AttributionAttemptIdentityV3Required(TypedDict):
    id: str
    number: int


class AttributionAttemptIdentityV3(_AttributionAttemptIdentityV3Required, total=False):
    retry_of: str


class _AttributionOperationErrorV3Required(TypedDict):
    type: str


class AttributionOperationErrorV3(_AttributionOperationErrorV3Required, total=False):
    """Error identity for a non-succeeded operation.

    ``type`` is canonical (``^[a-z0-9][a-z0-9._-]{0,127}$``); ``code`` is the
    optional provider error code (1-64 chars). The server rejects this object
    on an operation whose ``status`` is ``"succeeded"``.
    """

    code: str


class _AttributionOperationIdentityV3Required(TypedDict):
    id: str
    name: str
    status: AttributionOperationStatusV3
    attempt: AttributionAttemptIdentityV3


class AttributionOperationIdentityV3(_AttributionOperationIdentityV3Required, total=False):
    trace: AttributionTraceIdentityV3
    latency_ms: int
    error: AttributionOperationErrorV3


class _AttributionCostEvidenceV3Required(TypedDict):
    amount: str
    currency: str
    source: AttributionCostEvidenceSource
    confidence: AttributionConfidence


class AttributionCostEvidenceV3(_AttributionCostEvidenceV3Required, total=False):
    pricing_version: str


class AttributionLifecycleV3(TypedDict):
    state: AttributionLifecycleState
    revision: int


class _AttributionUsagePeriodV3Required(TypedDict):
    start_at: str


class AttributionUsagePeriodV3(_AttributionUsagePeriodV3Required, total=False):
    end_at: str


class _AttributionObservationV3Required(TypedDict):
    schema_version: Literal["3"]
    event_id: str
    task_id: str
    occurred_at: str
    observed_at: str
    component: AttributionComponent
    provider: AttributionProviderIdentityV3
    operation: AttributionOperationIdentityV3
    lifecycle: AttributionLifecycleV3
    usage_snapshot: Literal["full"]
    usage: list[AttributionUsageLineV3]


class AttributionObservationV3(_AttributionObservationV3Required, total=False):
    environment: str
    resource: AttributionResourceV3
    capability: AttributionCapabilityIdentityV3
    usage_period: AttributionUsagePeriodV3
    cost_evidence: AttributionCostEvidenceV3


AttributionEventV3 = AttributionObservationV3
