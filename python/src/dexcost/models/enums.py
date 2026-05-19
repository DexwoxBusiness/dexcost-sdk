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


class CostConfidence(str, Enum):
    """How trustworthy the reported cost_usd value is."""

    EXACT = "exact"
    COMPUTED = "computed"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PricingSource(str, Enum):
    """Where the cost_usd figure was derived from."""

    LITELLM = "litellm"
    TOKENCOST = "tokencost"
    PROVIDER_RESPONSE = "provider_response"
    MANUAL = "manual"
    CUSTOM = "custom"
    UNKNOWN = "unknown"
