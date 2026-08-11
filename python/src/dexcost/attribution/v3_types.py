"""Public attribution-v3 observation contract types."""

from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict

from dexcost.attribution.types import (
    AttributionComponent,
    AttributionConfidence,
    AttributionCostEvidenceSource,
    AttributionLifecycleState,
    AttributionResourceV2,
)

ATTRIBUTION_V3_CONTRACT_VERSION = "3.0.0"

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


AttributionResourceV3: TypeAlias = AttributionResourceV2


class AttributionTraceIdentityV3(TypedDict):
    trace_id: str
    span_id: str


class _AttributionAttemptIdentityV3Required(TypedDict):
    id: str
    number: int


class AttributionAttemptIdentityV3(_AttributionAttemptIdentityV3Required, total=False):
    retry_of: str


class _AttributionOperationIdentityV3Required(TypedDict):
    id: str
    name: str
    status: AttributionOperationStatusV3
    attempt: AttributionAttemptIdentityV3


class AttributionOperationIdentityV3(_AttributionOperationIdentityV3Required, total=False):
    trace: AttributionTraceIdentityV3


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
    resource: AttributionResourceV3
    usage_period: AttributionUsagePeriodV3
    cost_evidence: AttributionCostEvidenceV3


AttributionEventV3 = AttributionObservationV3
