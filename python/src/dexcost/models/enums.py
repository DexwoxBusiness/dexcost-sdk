"""Enumerated types used across dexcost data models."""

from enum import Enum


class TaskStatus(str, Enum):
    """Lifecycle status of a tracked task."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class EventType(str, Enum):
    """Discriminator for cost-generating events."""

    LLM_CALL = "llm_call"
    EXTERNAL_COST = "external_cost"
    COMPUTE_COST = "compute_cost"
    RETRY_MARKER = "retry_marker"
    NETWORK = "network"
    GPU_COST = "gpu_cost"
    GPU_UTILIZATION_SIGNAL = "gpu_utilization_signal"


class CostConfidence(str, Enum):
    """How trustworthy the reported cost_usd value is."""

    EXACT = "exact"
    COMPUTED = "computed"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PricingSource(str, Enum):
    """Where the cost_usd figure was derived from.

    Sprint 3 Theme F / §4.1.3 (P3): canonical 8-value set aligned
    across all 4 SDKs. Adding new values requires a coordinated wire-
    contract change — bump schema_version.
    """

    LITELLM = "litellm"
    TOKENCOST = "tokencost"
    PROVIDER_RESPONSE = "provider_response"
    MANUAL = "manual"
    CUSTOM = "custom"
    RATE_REGISTRY = "rate_registry"
    SERVICE_CATALOG = "service_catalog"
    UNKNOWN = "unknown"
