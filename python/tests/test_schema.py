"""Tests for Standard Event Schema v1 validation (US-002)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.schema import validate

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixture(name: str) -> Any:
    """Load a JSON fixture file by name."""
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------
# Helpers — build realistic model instances
# ------------------------------------------------------------------


def _make_task() -> Task:
    """Create a fully-populated Task for testing."""
    return Task(
        task_id=uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
        task_type="resolve_ticket",
        status="success",
        started_at=datetime(2025, 11, 15, 10, 30, 0, tzinfo=timezone.utc),
        ended_at=datetime(2025, 11, 15, 10, 32, 45, tzinfo=timezone.utc),
        metadata={"ticket_id": "SUPPORT-4521"},
        customer_id="cust_acme_corp",
        project_id="proj_helpdesk_ai",
        parent_task_id=uuid.UUID("f0e1d2c3-b4a5-6789-0fed-cba987654321"),
        llm_cost_usd=Decimal("0.0347"),
        external_cost_usd=Decimal("0.005"),
        compute_cost_usd=Decimal("0.0012"),
        total_cost_usd=Decimal("0.0409"),
        total_input_tokens=2450,
        total_output_tokens=680,
        total_cached_tokens=512,
        retry_count=1,
        retry_cost_usd=Decimal("0.0082"),
        failure_count=0,
    )


def _make_event() -> Event:
    """Create a fully-populated LLM call Event for testing."""
    return Event(
        event_id=uuid.UUID("b2c3d4e5-f6a7-8901-bcde-f12345678901"),
        task_id=uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
        event_type="llm_call",
        occurred_at=datetime(2025, 11, 15, 10, 30, 12, tzinfo=timezone.utc),
        cost_usd=Decimal("0.0265"),
        cost_confidence="exact",
        pricing_source="provider_response",
        pricing_version="2025-11-01",
        service_name="openai",
        provider="openai",
        model="gpt-4o",
        input_tokens=1850,
        output_tokens=420,
        cached_tokens=512,
        latency_ms=1340,
        is_retry=False,
        retry_reason=None,
        retry_of=None,
        details={"temperature": 0.7},
    )


# ------------------------------------------------------------------
# Valid payloads
# ------------------------------------------------------------------


class TestValidPayloads:
    """Payloads produced by model.to_dict() should always validate clean."""

    def test_valid_task_passes(self) -> None:
        task = _make_task()
        errors = validate(task.to_dict())
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_valid_event_passes(self) -> None:
        event = _make_event()
        errors = validate(event.to_dict())
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_minimal_task_passes(self) -> None:
        """A default Task() with no optional fields should validate."""
        task = Task()
        errors = validate(task.to_dict())
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_minimal_event_passes(self) -> None:
        """A default Event() with no optional fields should validate."""
        event = Event()
        errors = validate(event.to_dict())
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_network_event_type_passes(self) -> None:
        """event_type='network' is a valid enum value and should validate clean."""
        event = Event(event_type="network")
        errors = validate(event.to_dict())
        assert errors == [], f"Unexpected validation errors: {errors}"


# ------------------------------------------------------------------
# Invalid payloads
# ------------------------------------------------------------------


class TestInvalidPayloads:
    """Deliberately broken payloads should produce validation errors."""

    def test_invalid_task_missing_required(self) -> None:
        payload = _make_task().to_dict()
        del payload["task_id"]
        errors = validate(payload)
        assert len(errors) > 0
        assert any("task_id" in e for e in errors)

    def test_invalid_event_wrong_type(self) -> None:
        """cost_usd must be a string (Decimal format), not a float."""
        payload = _make_event().to_dict()
        payload["cost_usd"] = 0.0265  # wrong: float instead of string
        errors = validate(payload)
        assert len(errors) > 0
        assert any("cost_usd" in e for e in errors)

    def test_invalid_schema_version(self) -> None:
        payload = _make_task().to_dict()
        payload["schema_version"] = "99"
        errors = validate(payload)
        assert errors == ["Unsupported schema_version: 99"]

    def test_missing_task_id_and_event_id(self) -> None:
        errors = validate({"schema_version": "1"})
        assert errors == ["Cannot determine payload type: missing task_id or event_id"]

    def test_invalid_task_bad_status(self) -> None:
        payload = _make_task().to_dict()
        payload["status"] = "cancelled"  # not in enum
        errors = validate(payload)
        assert len(errors) > 0
        assert any("status" in e for e in errors)

    def test_invalid_event_bad_event_type(self) -> None:
        payload = _make_event().to_dict()
        payload["event_type"] = "unknown_type"  # not in enum
        errors = validate(payload)
        assert len(errors) > 0
        assert any("event_type" in e for e in errors)

    def test_invalid_task_extra_field(self) -> None:
        """additionalProperties: false should reject unknown fields."""
        payload = _make_task().to_dict()
        payload["surprise_field"] = "oops"
        errors = validate(payload)
        assert len(errors) > 0
        assert any("surprise_field" in e or "Additional" in e for e in errors)

    def test_invalid_event_extra_field(self) -> None:
        payload = _make_event().to_dict()
        payload["surprise_field"] = "oops"
        errors = validate(payload)
        assert len(errors) > 0
        assert any("surprise_field" in e or "Additional" in e for e in errors)


# ------------------------------------------------------------------
# Fixture validation
# ------------------------------------------------------------------


class TestFixtureValidation:
    """All fixture files should validate against the v1 schemas."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "task.v1.json",
            "event_llm_call.v1.json",
            "event_external_cost.v1.json",
            "event_late.v1.json",
        ],
    )
    def test_single_payload_fixtures_validate(self, fixture_name: str) -> None:
        payload: dict[str, Any] = _load_fixture(fixture_name)
        errors = validate(payload)
        assert errors == [], f"{fixture_name}: {errors}"

    def test_batch_payload_fixture_validates(self) -> None:
        payloads: list[dict[str, Any]] = _load_fixture("batch_payload.v1.json")
        for i, payload in enumerate(payloads):
            errors = validate(payload)
            assert errors == [], f"batch_payload.v1.json[{i}]: {errors}"

    def test_ingest_request_records_validate(self) -> None:
        data: dict[str, Any] = _load_fixture("ingest_request.v1.json")
        for i, record in enumerate(data["records"]):
            # Strip record_type before validation (not part of schema)
            payload = {k: v for k, v in record.items() if k != "record_type"}
            errors = validate(payload)
            assert errors == [], f"ingest_request.v1.json records[{i}]: {errors}"

    def test_ingest_response_fixture_structure(self) -> None:
        data: dict[str, Any] = _load_fixture("ingest_response.v1.json")
        assert data["accepted"] == 3
        assert data["rejected"] == 0
        assert data["errors"] == []


# ------------------------------------------------------------------
# Round-trip tests
# ------------------------------------------------------------------


class TestRoundTrip:
    """Verify to_dict -> validate -> from_dict -> to_dict is stable."""

    def test_round_trip_task(self) -> None:
        original = _make_task()
        d1 = original.to_dict()
        errors = validate(d1)
        assert errors == [], f"Validation errors: {errors}"

        restored = Task.from_dict(d1)
        d2 = restored.to_dict()
        assert d1 == d2

    def test_round_trip_event(self) -> None:
        original = _make_event()
        d1 = original.to_dict()
        errors = validate(d1)
        assert errors == [], f"Validation errors: {errors}"

        restored = Event.from_dict(d1)
        d2 = restored.to_dict()
        assert d1 == d2

    def test_round_trip_minimal_task(self) -> None:
        original = Task()
        d1 = original.to_dict()
        errors = validate(d1)
        assert errors == [], f"Validation errors: {errors}"

        restored = Task.from_dict(d1)
        d2 = restored.to_dict()
        assert d1 == d2

    def test_round_trip_minimal_event(self) -> None:
        original = Event()
        d1 = original.to_dict()
        errors = validate(d1)
        assert errors == [], f"Validation errors: {errors}"

        restored = Event.from_dict(d1)
        d2 = restored.to_dict()
        assert d1 == d2
