"""Tests for non-LLM cost recording (US-016).

Validates:
- record_cost with default event_type ("external_cost")
- record_cost with explicit event_type="compute_cost"
- Optional metadata (endpoint, method, region, duration_ms) in details
- cost_confidence defaults to "exact" for manual recording
- Events appear in task total alongside LLM costs
- Queryable separately by event_type and customer_id
- Mixed LLM + external + compute costs sum to task.total_cost_usd
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_instrument=[])


# ---------------------------------------------------------------------------
# AC1: record_cost records an external_cost event by default
# ---------------------------------------------------------------------------


class TestRecordCostExternalDefault:
    """task.record_cost(service=..., cost_usd=...) records an external_cost event."""

    def test_default_event_type_is_external_cost(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="api_call", customer_id="acme")
        event = task.record_cost(service="google_maps_api", cost_usd="0.005")
        task.end()

        assert event.event_type == "external_cost"
        assert event.cost_usd == Decimal("0.005")
        assert event.service_name == "google_maps_api"

    def test_decimal_cost_usd_accepted(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="decimal_test")
        event = task.record_cost(service="twilio_sms", cost_usd=Decimal("0.0075"))
        task.end()

        assert event.cost_usd == Decimal("0.0075")

    def test_float_like_string_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="str_cost")
        event = task.record_cost(service="ocr_api", cost_usd="0.015")
        task.end()

        assert event.cost_usd == Decimal("0.015")


# ---------------------------------------------------------------------------
# AC2: record_cost with event_type="compute_cost"
# ---------------------------------------------------------------------------


class TestRecordCostComputeType:
    """task.record_cost(..., event_type="compute_cost") for compute costs."""

    def test_compute_cost_event_type(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="lambda_run")
        event = task.record_cost(
            service="aws_lambda",
            cost_usd="0.0003",
            event_type="compute_cost",
        )
        task.end()

        assert event.event_type == "compute_cost"
        assert event.cost_usd == Decimal("0.0003")
        assert event.service_name == "aws_lambda"

    def test_compute_cost_aggregated(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="compute_agg")
        task.record_cost(
            service="aws_lambda",
            cost_usd="0.0003",
            event_type="compute_cost",
        )
        task.record_cost(
            service="gpu_inference",
            cost_usd="0.05",
            event_type="compute_cost",
        )
        task.end()

        tasks = storage.query_tasks(task_type="compute_agg")
        t = tasks[0]
        assert t.compute_cost_usd == Decimal("0.0503")
        assert t.total_cost_usd == Decimal("0.0503")
        assert t.external_cost_usd == Decimal("0")

    def test_invalid_event_type_raises(self, tracker: CostTracker) -> None:
        task = tracker.start_task(task_type="bad_type")
        with pytest.raises(ValueError, match="event_type must be one of"):
            task.record_cost(service="foo", cost_usd="1.00", event_type="llm_call")
        task.end()


# ---------------------------------------------------------------------------
# AC3: Optional metadata stored in details
# ---------------------------------------------------------------------------


class TestRecordCostMetadata:
    """Optional metadata: endpoint, method, region, duration_ms in details."""

    def test_metadata_fields_persisted(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="meta_test")
        event = task.record_cost(
            service="google_maps_api",
            cost_usd="0.005",
            details={
                "endpoint": "/v1/geocode",
                "method": "GET",
                "region": "us-east-1",
                "duration_ms": 142,
            },
        )
        task.end()

        # Verify details on the returned event
        assert event.details["endpoint"] == "/v1/geocode"
        assert event.details["method"] == "GET"
        assert event.details["region"] == "us-east-1"
        assert event.details["duration_ms"] == 142

        # Verify details round-trip through storage
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        stored = events[0]
        assert stored.details["endpoint"] == "/v1/geocode"
        assert stored.details["method"] == "GET"
        assert stored.details["region"] == "us-east-1"
        assert stored.details["duration_ms"] == 142

    def test_compute_cost_with_metadata(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="compute_meta")
        event = task.record_cost(
            service="aws_lambda",
            cost_usd="0.0003",
            event_type="compute_cost",
            details={
                "function_name": "process-image",
                "region": "us-west-2",
                "duration_ms": 850,
                "memory_mb": 256,
            },
        )
        task.end()

        assert event.event_type == "compute_cost"
        assert event.details["function_name"] == "process-image"
        assert event.details["duration_ms"] == 850

    def test_empty_details_default(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="no_details")
        event = task.record_cost(service="sms_api", cost_usd="0.01")
        task.end()

        assert event.details == {}


# ---------------------------------------------------------------------------
# AC4: cost_confidence defaults to "exact" for manual recording
# ---------------------------------------------------------------------------


class TestCostConfidenceDefault:
    """cost_confidence defaults to 'exact' for manual recording."""

    def test_default_confidence_is_exact(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="conf_test")
        event = task.record_cost(service="maps_api", cost_usd="0.005")
        task.end()

        assert event.cost_confidence == "exact"

    def test_default_pricing_source_is_manual(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="src_test")
        event = task.record_cost(service="maps_api", cost_usd="0.005")
        task.end()

        assert event.pricing_source == "manual"

    def test_confidence_overridable(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="conf_override")
        event = task.record_cost(
            service="maps_api",
            cost_usd="0.005",
            cost_confidence="estimated",
        )
        task.end()

        assert event.cost_confidence == "estimated"


# ---------------------------------------------------------------------------
# AC5: Events appear in task total alongside LLM costs
# ---------------------------------------------------------------------------


class TestMixedCostsTotal:
    """Events appear in task total alongside LLM costs."""

    def test_external_in_total(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="ext_total", customer_id="cust-1")
        task.record_cost(service="maps_api", cost_usd="0.005")
        task.end()

        tasks = storage.query_tasks(task_type="ext_total")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.005")
        assert t.total_cost_usd == Decimal("0.005")

    def test_compute_in_total(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="cmp_total")
        task.record_cost(service="lambda", cost_usd="0.001", event_type="compute_cost")
        task.end()

        tasks = storage.query_tasks(task_type="cmp_total")
        t = tasks[0]
        assert t.compute_cost_usd == Decimal("0.001")
        assert t.total_cost_usd == Decimal("0.001")


# ---------------------------------------------------------------------------
# AC6: Queryable separately — "show me only external costs for customer X"
# ---------------------------------------------------------------------------


class TestQueryByCostTypeAndCustomer:
    """Queryable separately: filter by event_type and customer_id."""

    def test_query_external_costs_for_customer(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        # Customer A: external + compute costs
        task_a = tracker.start_task(task_type="work_a", customer_id="customer-a")
        task_a.record_cost(service="maps", cost_usd="0.01")
        task_a.record_cost(service="lambda", cost_usd="0.002", event_type="compute_cost")
        task_a.end()

        # Customer B: external cost
        task_b = tracker.start_task(task_type="work_b", customer_id="customer-b")
        task_b.record_cost(service="sms", cost_usd="0.05")
        task_b.end()

        # Query: external costs for customer-a only
        events = storage.query_events(event_type="external_cost", customer_id="customer-a")
        assert len(events) == 1
        assert events[0].service_name == "maps"
        assert events[0].cost_usd == Decimal("0.01")

    def test_query_compute_costs_for_customer(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="compute_q", customer_id="customer-x")
        task.record_cost(service="gpu", cost_usd="0.10", event_type="compute_cost")
        task.record_cost(service="maps", cost_usd="0.01")
        task.end()

        events = storage.query_events(event_type="compute_cost", customer_id="customer-x")
        assert len(events) == 1
        assert events[0].service_name == "gpu"

    def test_query_all_events_for_customer(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="all_q", customer_id="customer-y")
        task.record_cost(service="maps", cost_usd="0.01")
        task.record_cost(service="lambda", cost_usd="0.002", event_type="compute_cost")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        events = storage.query_events(customer_id="customer-y")
        assert len(events) == 3

    def test_query_returns_empty_for_wrong_customer(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="wrong_q", customer_id="customer-z")
        task.record_cost(service="sms", cost_usd="0.03")
        task.end()

        events = storage.query_events(event_type="external_cost", customer_id="nonexistent")
        assert len(events) == 0


# ---------------------------------------------------------------------------
# AC7: Mixed LLM + external + compute costs — total_cost_usd is sum of all
# ---------------------------------------------------------------------------


class TestMixedCostsSumToTotal:
    """Record mixed LLM + external + compute costs, verify total is sum."""

    def test_mixed_costs_sum_via_record_cost(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="mixed_sum", customer_id="acme")
        # LLM call
        task.record_llm_call("openai", "gpt-4", 200, 100, "0.10")
        # External cost (default event_type)
        task.record_cost(service="google_maps_api", cost_usd="0.005")
        # Compute cost
        task.record_cost(
            service="aws_lambda",
            cost_usd="0.0003",
            event_type="compute_cost",
        )
        task.end()

        tasks = storage.query_tasks(task_type="mixed_sum")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.10")
        assert t.external_cost_usd == Decimal("0.005")
        assert t.compute_cost_usd == Decimal("0.0003")
        expected_total = Decimal("0.10") + Decimal("0.005") + Decimal("0.0003")
        assert t.total_cost_usd == expected_total
        assert t.total_input_tokens == 200
        assert t.total_output_tokens == 100

    def test_mixed_costs_via_context_manager(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        with tracker.task(task_type="cm_mixed", customer_id="beta") as task:
            task.record_llm_call("anthropic", "claude-3", 150, 75, "0.08")
            task.record_cost(service="search_api", cost_usd="0.02")
            task.record_cost(
                service="gpu_inference",
                cost_usd="0.05",
                event_type="compute_cost",
            )

        tasks = storage.query_tasks(task_type="cm_mixed")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.08")
        assert t.external_cost_usd == Decimal("0.02")
        assert t.compute_cost_usd == Decimal("0.05")
        expected_total = Decimal("0.08") + Decimal("0.02") + Decimal("0.05")
        assert t.total_cost_usd == expected_total

    def test_multiple_of_each_type(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="multi_each")
        # Two LLM calls
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.record_llm_call("anthropic", "claude-3", 200, 100, "0.08")
        # Two external costs
        task.record_cost(service="maps", cost_usd="0.01")
        task.record_cost(service="sms", cost_usd="0.02")
        # Two compute costs
        task.record_cost(service="lambda_1", cost_usd="0.003", event_type="compute_cost")
        task.record_cost(service="lambda_2", cost_usd="0.004", event_type="compute_cost")
        task.end()

        tasks = storage.query_tasks(task_type="multi_each")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.13")
        assert t.external_cost_usd == Decimal("0.03")
        assert t.compute_cost_usd == Decimal("0.007")
        assert t.total_cost_usd == Decimal("0.167")


# ---------------------------------------------------------------------------
# One-liner API frictionless test
# ---------------------------------------------------------------------------


class TestOneLineAPI:
    """API must be as frictionless as possible — one line per cost."""

    def test_one_line_external(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Matches AC1 example exactly."""
        task = tracker.start_task(task_type="one_liner")
        task.record_cost(service="google_maps_api", cost_usd=Decimal("0.005"))
        task.end()

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].event_type == "external_cost"
        assert events[0].cost_usd == Decimal("0.005")

    def test_one_line_compute(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Matches AC2 example exactly."""
        task = tracker.start_task(task_type="one_liner_compute")
        task.record_cost(
            service="aws_lambda",
            cost_usd=Decimal("0.0003"),
            event_type="compute_cost",
        )
        task.end()

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].event_type == "compute_cost"
        assert events[0].cost_usd == Decimal("0.0003")
