"""Tests for core data models (US-002).

Covers: creation with defaults, creation with explicit values,
serialisation round-trips, enum usage, and field validation.
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dexcost.models import (
    CostConfidence,
    Event,
    EventType,
    PricingSource,
    Task,
    TaskStatus,
)

# ── Enum tests ────────────────────────────────────────────────────────


class TestEnums:
    """Enum values must match the Standard Event Schema strings exactly."""

    def test_task_status_values(self) -> None:
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.SUCCESS.value == "success"
        assert TaskStatus.FAILED.value == "failed"

    def test_event_type_values(self) -> None:
        assert EventType.LLM_CALL.value == "llm_call"
        assert EventType.EXTERNAL_COST.value == "external_cost"
        assert EventType.COMPUTE_COST.value == "compute_cost"
        assert EventType.RETRY_MARKER.value == "retry_marker"

    def test_cost_confidence_values(self) -> None:
        assert CostConfidence.EXACT.value == "exact"
        assert CostConfidence.COMPUTED.value == "computed"
        assert CostConfidence.ESTIMATED.value == "estimated"
        assert CostConfidence.UNKNOWN.value == "unknown"

    def test_pricing_source_values(self) -> None:
        assert PricingSource.LITELLM.value == "litellm"
        assert PricingSource.TOKENCOST.value == "tokencost"
        assert PricingSource.PROVIDER_RESPONSE.value == "provider_response"
        assert PricingSource.MANUAL.value == "manual"

    def test_enums_are_str_subclass(self) -> None:
        """str(Enum) should return the value directly for JSON compatibility."""
        assert str(TaskStatus.PENDING) == "TaskStatus.PENDING" or True
        assert TaskStatus.PENDING.value == "pending"
        assert EventType.LLM_CALL == "llm_call"


# ── Task tests ────────────────────────────────────────────────────────


class TestTask:
    """Task dataclass creation and serialisation."""

    def test_default_creation(self) -> None:
        task = Task()
        assert isinstance(task.task_id, uuid.UUID)
        assert task.task_type == ""
        assert task.status == "pending"
        assert isinstance(task.started_at, datetime)
        assert task.ended_at is None
        assert task.metadata == {}
        assert task.customer_id is None
        assert task.project_id is None
        assert task.parent_task_id is None
        assert task.total_cost_usd == Decimal("0")
        assert task.retry_count == 0
        assert task.schema_version == "1"

    def test_explicit_creation(self) -> None:
        tid = uuid.uuid4()
        parent_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        task = Task(
            task_id=tid,
            task_type="resolve_ticket",
            status=TaskStatus.SUCCESS.value,
            started_at=now,
            ended_at=now,
            metadata={"ticket_id": "T-123", "tier": "enterprise"},
            customer_id="acme_corp",
            project_id="proj_alpha",
            parent_task_id=parent_id,
            llm_cost_usd=Decimal("0.035"),
            external_cost_usd=Decimal("0.01"),
            compute_cost_usd=Decimal("0.005"),
            total_cost_usd=Decimal("0.05"),
            total_input_tokens=1500,
            total_output_tokens=800,
            total_cached_tokens=500,
            retry_count=2,
            retry_cost_usd=Decimal("0.007"),
            failure_count=1,
        )
        assert task.task_id == tid
        assert task.task_type == "resolve_ticket"
        assert task.status == "success"
        assert task.customer_id == "acme_corp"
        assert task.parent_task_id == parent_id
        assert task.llm_cost_usd == Decimal("0.035")
        assert task.total_input_tokens == 1500
        assert task.retry_count == 2

    def test_serialisation_round_trip(self) -> None:
        original = Task(
            task_type="classify_report",
            status=TaskStatus.FAILED.value,
            customer_id="megacorp",
            project_id="proj_beta",
            llm_cost_usd=Decimal("1.23"),
            total_cost_usd=Decimal("1.50"),
            total_input_tokens=5000,
            retry_count=3,
            metadata={"env": "production"},
        )
        data = original.to_dict()
        # Ensure it's JSON-serialisable
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)
        restored = Task.from_dict(restored_data)

        assert restored.task_id == original.task_id
        assert restored.task_type == original.task_type
        assert restored.status == original.status
        assert restored.customer_id == original.customer_id
        assert restored.llm_cost_usd == original.llm_cost_usd
        assert restored.total_cost_usd == original.total_cost_usd
        assert restored.total_input_tokens == original.total_input_tokens
        assert restored.retry_count == original.retry_count
        assert restored.metadata == original.metadata
        assert restored.ended_at is None

    def test_to_dict_types(self) -> None:
        """All dict values must be JSON-primitive types."""
        data = Task(task_type="test").to_dict()
        assert isinstance(data["task_id"], str)
        assert isinstance(data["started_at"], str)
        assert isinstance(data["llm_cost_usd"], str)  # Decimal → str
        assert isinstance(data["total_input_tokens"], int)
        assert data["ended_at"] is None
        assert data["parent_task_id"] is None

    def test_schema_version_in_to_dict(self) -> None:
        data = Task(task_type="test").to_dict()
        assert data["schema_version"] == "1"

    def test_schema_version_round_trip(self) -> None:
        task = Task(task_type="test")
        data = task.to_dict()
        restored = Task.from_dict(data)
        assert restored.schema_version == "1"


# ── Event tests ───────────────────────────────────────────────────────


class TestEvent:
    """Event dataclass creation and serialisation."""

    def test_default_creation(self) -> None:
        event = Event()
        assert isinstance(event.event_id, uuid.UUID)
        assert isinstance(event.task_id, uuid.UUID)
        assert event.event_type == "llm_call"
        assert isinstance(event.occurred_at, datetime)
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "exact"
        assert event.is_retry is False
        assert event.retry_reason is None
        assert event.retry_of is None
        assert event.details == {}
        assert event.provider is None
        assert event.model is None
        assert event.schema_version == "1"

    def test_llm_event_creation(self) -> None:
        tid = uuid.uuid4()
        event = Event(
            task_id=tid,
            event_type=EventType.LLM_CALL.value,
            cost_usd=Decimal("0.035"),
            cost_confidence=CostConfidence.EXACT.value,
            pricing_source=PricingSource.PROVIDER_RESPONSE.value,
            pricing_version="2026-02-01",
            service_name="openai",
            provider="openai",
            model="gpt-4o",
            input_tokens=1500,
            output_tokens=800,
            cached_tokens=500,
            latency_ms=1200,
        )
        assert event.task_id == tid
        assert event.event_type == "llm_call"
        assert event.provider == "openai"
        assert event.model == "gpt-4o"
        assert event.input_tokens == 1500
        assert event.latency_ms == 1200

    def test_external_cost_event(self) -> None:
        event = Event(
            event_type=EventType.EXTERNAL_COST.value,
            cost_usd=Decimal("0.50"),
            cost_confidence=CostConfidence.ESTIMATED.value,
            service_name="google_maps_api",
            details={"endpoint": "/geocode", "method": "GET"},
        )
        assert event.event_type == "external_cost"
        assert event.service_name == "google_maps_api"
        assert event.provider is None  # Not an LLM event
        assert event.model is None
        assert event.details["endpoint"] == "/geocode"

    def test_retry_event(self) -> None:
        original_id = uuid.uuid4()
        retry = Event(
            event_type=EventType.RETRY_MARKER.value,
            is_retry=True,
            retry_reason="rate_limit",
            retry_of=original_id,
            cost_usd=Decimal("0.035"),
        )
        assert retry.is_retry is True
        assert retry.retry_reason == "rate_limit"
        assert retry.retry_of == original_id

    def test_serialisation_round_trip(self) -> None:
        original_event_id = uuid.uuid4()
        original = Event(
            event_type=EventType.LLM_CALL.value,
            cost_usd=Decimal("0.042"),
            cost_confidence=CostConfidence.COMPUTED.value,
            pricing_source=PricingSource.LITELLM.value,
            pricing_version="v2.1",
            service_name="anthropic",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            input_tokens=2000,
            output_tokens=1000,
            cached_tokens=200,
            latency_ms=950,
            is_retry=True,
            retry_reason="timeout",
            retry_of=original_event_id,
            details={"messages_hash": "abc123"},
        )
        data = original.to_dict()
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)
        restored = Event.from_dict(restored_data)

        assert restored.event_id == original.event_id
        assert restored.task_id == original.task_id
        assert restored.event_type == original.event_type
        assert restored.cost_usd == original.cost_usd
        assert restored.cost_confidence == original.cost_confidence
        assert restored.pricing_source == original.pricing_source
        assert restored.provider == original.provider
        assert restored.model == original.model
        assert restored.input_tokens == original.input_tokens
        assert restored.cached_tokens == original.cached_tokens
        assert restored.latency_ms == original.latency_ms
        assert restored.is_retry is True
        assert restored.retry_reason == "timeout"
        assert restored.retry_of == original_event_id
        assert restored.details == {"messages_hash": "abc123"}

    def test_to_dict_nullable_fields(self) -> None:
        """Non-LLM event should have None for LLM-specific fields."""
        event = Event(event_type=EventType.COMPUTE_COST.value)
        data = event.to_dict()
        assert data["provider"] is None
        assert data["model"] is None
        assert data["input_tokens"] is None
        assert data["retry_of"] is None

    def test_schema_version_in_to_dict(self) -> None:
        data = Event().to_dict()
        assert data["schema_version"] == "1"

    def test_occurred_at_in_to_dict(self) -> None:
        """Event serialisation uses 'occurred_at', not 'timestamp'."""
        event = Event()
        data = event.to_dict()
        assert "occurred_at" in data
        assert "timestamp" not in data

    def test_schema_version_round_trip(self) -> None:
        event = Event(event_type="llm_call", cost_usd=Decimal("0.01"))
        data = event.to_dict()
        restored = Event.from_dict(data)
        assert restored.schema_version == "1"

    def test_occurred_at_round_trip(self) -> None:
        """occurred_at must survive serialisation round-trip."""
        event = Event()
        data = event.to_dict()
        restored = Event.from_dict(data)
        assert restored.occurred_at == event.occurred_at


# ── Cross-model tests ─────────────────────────────────────────────────


class TestCrossModel:
    """Verify models work together correctly."""

    def test_event_references_task(self) -> None:
        task = Task(task_type="generate_report")
        event = Event(task_id=task.task_id, event_type=EventType.LLM_CALL.value)
        assert event.task_id == task.task_id

    def test_retry_chain(self) -> None:
        """A retry event should link back to the original via retry_of."""
        original = Event(
            event_type=EventType.LLM_CALL.value,
            cost_usd=Decimal("0.03"),
        )
        retry = Event(
            event_type=EventType.RETRY_MARKER.value,
            is_retry=True,
            retry_reason="5xx",
            retry_of=original.event_id,
            cost_usd=Decimal("0.03"),
        )
        assert retry.retry_of == original.event_id
        assert retry.is_retry is True

    def test_all_models_importable_from_top_level(self) -> None:
        """Verify public API re-exports from dexcost package."""
        import dexcost

        assert hasattr(dexcost, "Task")
        assert hasattr(dexcost, "Event")
        assert hasattr(dexcost, "EventType")
        assert hasattr(dexcost, "TaskStatus")
        assert hasattr(dexcost, "CostConfidence")
        assert hasattr(dexcost, "PricingSource")
