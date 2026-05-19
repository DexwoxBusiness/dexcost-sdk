"""Tests for the Cost Rates Registry (US-011).

Covers: register_rate, record_usage, load_rates, export_rates,
pricing_version tracking, error messages, and YAML round-trip.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.rates import RateEntry, RateRegistry
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    """Create a fresh SQLite storage for each test."""
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    """Create a CostTracker backed by the tmp-based storage."""
    return CostTracker(storage=storage, auto_instrument=[])


# ---------------------------------------------------------------------------
# RateRegistry unit tests
# ---------------------------------------------------------------------------


class TestRateRegistry:
    """Low-level RateRegistry tests."""

    def test_register_and_get(self) -> None:
        reg = RateRegistry()
        reg.register("maps.googleapis.com", per="request", cost_usd="0.005")
        entry = reg.get("maps.googleapis.com")
        assert entry is not None
        assert entry.service == "maps.googleapis.com"
        assert entry.per == "request"
        assert entry.cost_usd == Decimal("0.005")

    def test_get_missing_returns_none(self) -> None:
        reg = RateRegistry()
        assert reg.get("nonexistent") is None

    def test_overwrite_rate(self) -> None:
        reg = RateRegistry()
        reg.register("api.example.com", per="request", cost_usd="0.01")
        reg.register("api.example.com", per="request", cost_usd="0.02")
        entry = reg.get("api.example.com")
        assert entry is not None
        assert entry.cost_usd == Decimal("0.02")

    def test_rates_property_returns_copy(self) -> None:
        reg = RateRegistry()
        reg.register("svc", per="call", cost_usd="0.001")
        rates = reg.rates
        assert "svc" in rates
        # Mutating the copy should not affect the registry
        rates.pop("svc")
        assert reg.get("svc") is not None

    def test_pricing_version_deterministic(self) -> None:
        reg1 = RateRegistry()
        reg1.register("a", per="req", cost_usd="0.01")
        reg1.register("b", per="page", cost_usd="0.02")

        reg2 = RateRegistry()
        reg2.register("b", per="page", cost_usd="0.02")
        reg2.register("a", per="req", cost_usd="0.01")

        # Same rates in different order → same version
        assert reg1.pricing_version == reg2.pricing_version

    def test_pricing_version_changes_on_register(self) -> None:
        reg = RateRegistry()
        reg.register("svc1", per="req", cost_usd="0.01")
        v1 = reg.pricing_version
        reg.register("svc2", per="page", cost_usd="0.02")
        v2 = reg.pricing_version
        assert v1 != v2

    def test_pricing_version_is_12_char_hex(self) -> None:
        reg = RateRegistry()
        reg.register("svc", per="req", cost_usd="0.01")
        v = reg.pricing_version
        assert len(v) == 12
        int(v, 16)  # Validates it's hex


# ---------------------------------------------------------------------------
# YAML load/export tests
# ---------------------------------------------------------------------------


class TestRateRegistryYAML:
    """Load and export YAML config files."""

    def test_load_rates(self, tmp_path: Path) -> None:
        yaml_content = """\
rates:
  maps.googleapis.com:
    per: request
    cost_usd: "0.005"
  ocr-api.com:
    per: page
    cost_usd: "0.01"
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        reg = RateRegistry()
        reg.load(yaml_file)

        maps = reg.get("maps.googleapis.com")
        assert maps is not None
        assert maps.per == "request"
        assert maps.cost_usd == Decimal("0.005")

        ocr = reg.get("ocr-api.com")
        assert ocr is not None
        assert ocr.per == "page"
        assert ocr.cost_usd == Decimal("0.01")

    def test_export_rates(self, tmp_path: Path) -> None:
        reg = RateRegistry()
        reg.register("maps.googleapis.com", per="request", cost_usd="0.005")
        reg.register("ocr-api.com", per="page", cost_usd="0.01")

        yaml_file = tmp_path / "rates.yaml"
        reg.export(yaml_file)

        content = yaml_file.read_text(encoding="utf-8")
        assert "maps.googleapis.com" in content
        assert "ocr-api.com" in content
        assert "request" in content
        assert "page" in content

    def test_round_trip_load_export(self, tmp_path: Path) -> None:
        """Register → export → load into fresh registry → verify identical."""
        reg1 = RateRegistry()
        reg1.register("maps.googleapis.com", per="request", cost_usd="0.005")
        reg1.register("ocr-api.com", per="page", cost_usd="0.01")

        yaml_file = tmp_path / "rates.yaml"
        reg1.export(yaml_file)

        reg2 = RateRegistry()
        reg2.load(yaml_file)

        assert reg1.pricing_version == reg2.pricing_version
        assert reg2.get("maps.googleapis.com") == reg1.get("maps.googleapis.com")
        assert reg2.get("ocr-api.com") == reg1.get("ocr-api.com")

    def test_load_invalid_yaml_missing_cost(self, tmp_path: Path) -> None:
        yaml_content = """\
rates:
  bad-service:
    per: request
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        reg = RateRegistry()
        with pytest.raises(ValueError, match="cost_usd"):
            reg.load(yaml_file)

    def test_load_invalid_yaml_bad_structure(self, tmp_path: Path) -> None:
        yaml_content = """\
rates: "not a mapping"
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        reg = RateRegistry()
        with pytest.raises(ValueError, match="mapping"):
            reg.load(yaml_file)

    def test_load_merges_with_existing(self, tmp_path: Path) -> None:
        """Loading a file merges into existing rates (does not clear)."""
        reg = RateRegistry()
        reg.register("existing-svc", per="call", cost_usd="0.001")

        yaml_content = """\
rates:
  new-svc:
    per: request
    cost_usd: "0.005"
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")
        reg.load(yaml_file)

        assert reg.get("existing-svc") is not None
        assert reg.get("new-svc") is not None

    def test_load_default_per_unit(self, tmp_path: Path) -> None:
        """If 'per' is missing in YAML, defaults to 'unit'."""
        yaml_content = """\
rates:
  simple-svc:
    cost_usd: "0.005"
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        reg = RateRegistry()
        reg.load(yaml_file)
        entry = reg.get("simple-svc")
        assert entry is not None
        assert entry.per == "unit"

    def test_export_sorted_by_service_name(self, tmp_path: Path) -> None:
        reg = RateRegistry()
        reg.register("z-service", per="call", cost_usd="0.01")
        reg.register("a-service", per="req", cost_usd="0.02")

        yaml_file = tmp_path / "rates.yaml"
        reg.export(yaml_file)

        content = yaml_file.read_text(encoding="utf-8")
        a_pos = content.index("a-service")
        z_pos = content.index("z-service")
        assert a_pos < z_pos


# ---------------------------------------------------------------------------
# CostTracker rate registry integration tests
# ---------------------------------------------------------------------------


class TestCostTrackerRateRegistry:
    """CostTracker.register_rate / load_rates / export_rates."""

    def test_register_rate_basic(self, tracker: CostTracker) -> None:
        tracker.register_rate(service="maps.googleapis.com", per="request", cost_usd="0.005")
        rate = tracker.get_rate("maps.googleapis.com")
        assert rate == Decimal("0.005")

    def test_register_rate_different_units(self, tracker: CostTracker) -> None:
        tracker.register_rate(service="ocr-api.com", per="page", cost_usd="0.01")
        rate = tracker.get_rate("ocr-api.com")
        assert rate == Decimal("0.01")

    def test_get_rate_missing(self, tracker: CostTracker) -> None:
        assert tracker.get_rate("nonexistent") is None

    def test_load_and_export_rates(self, tracker: CostTracker, tmp_path: Path) -> None:
        tracker.register_rate(service="svc-a", per="request", cost_usd="0.005")
        tracker.register_rate(service="svc-b", per="page", cost_usd="0.01")

        out_file = tmp_path / "rates.yaml"
        tracker.export_rates(out_file)
        assert out_file.exists()

        # Load into a new tracker
        tracker2 = CostTracker(
            storage=SQLiteStorage(db_path=tmp_path / "test2.db"),
            auto_instrument=[],
        )
        tracker2.load_rates(out_file)
        assert tracker2.get_rate("svc-a") == Decimal("0.005")
        assert tracker2.get_rate("svc-b") == Decimal("0.01")

    def test_rate_registry_property(self, tracker: CostTracker) -> None:
        assert isinstance(tracker.rate_registry, RateRegistry)


# ---------------------------------------------------------------------------
# TrackedTask.record_usage with rate registry (US-011)
# ---------------------------------------------------------------------------


class TestRecordUsageWithRates:
    """record_usage() looks up rate, records cost event with pricing_version."""

    def test_record_usage_single_unit(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """record_usage(service) with default units=1."""
        tracker.register_rate(service="maps.googleapis.com", per="request", cost_usd="0.005")

        with tracker.task(task_type="usage_single") as task:
            event = task.record_usage(service="maps.googleapis.com")

        assert event.cost_usd == Decimal("0.005")
        assert event.service_name == "maps.googleapis.com"
        assert event.pricing_source == "rate_registry"
        assert event.pricing_version is not None
        assert len(event.pricing_version) == 12

        tasks = storage.query_tasks(task_type="usage_single")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.005")
        assert t.total_cost_usd == Decimal("0.005")

    def test_record_usage_multiple_units(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """record_usage(service, units=3) records 3 * rate."""
        tracker.register_rate(service="ocr-api.com", per="page", cost_usd="0.01")

        with tracker.task(task_type="usage_multi") as task:
            event = task.record_usage(service="ocr-api.com", units=3)

        assert event.cost_usd == Decimal("0.03")
        assert event.service_name == "ocr-api.com"
        assert event.pricing_source == "rate_registry"

        tasks = storage.query_tasks(task_type="usage_multi")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.03")
        assert t.total_cost_usd == Decimal("0.03")

    def test_record_usage_unregistered_service_error(self, tracker: CostTracker) -> None:
        """Clear error if service not registered, suggesting register_rate()."""
        with (
            tracker.task(task_type="usage_err") as task,
            pytest.raises(ValueError, match=r"No rate registered.*register_rate") as exc_info,
        ):
            task.record_usage(service="unknown-api.com")

        # Error message should include the service name and suggest register_rate
        msg = str(exc_info.value)
        assert "unknown-api.com" in msg
        assert "register_rate" in msg

    def test_record_usage_pricing_version_matches_registry(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Event pricing_version matches the registry's pricing_version."""
        tracker.register_rate(service="svc", per="call", cost_usd="0.001")
        expected_version = tracker.rate_registry.pricing_version

        with tracker.task(task_type="usage_pv") as task:
            event = task.record_usage(service="svc", units=2)

        assert event.pricing_version == expected_version

    def test_record_usage_cost_confidence_is_computed(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Events from record_usage have cost_confidence='computed'."""
        tracker.register_rate(service="svc", per="call", cost_usd="0.001")

        with tracker.task(task_type="usage_cc") as task:
            event = task.record_usage(service="svc")

        assert event.cost_confidence == "computed"

    def test_record_usage_with_details(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """record_usage passes details through to the event."""
        tracker.register_rate(service="svc", per="call", cost_usd="0.01")

        with tracker.task(task_type="usage_details") as task:
            event = task.record_usage(service="svc", units=2, details={"region": "us-east-1"})

        assert event.details == {"region": "us-east-1"}
        assert event.cost_usd == Decimal("0.02")

    def test_record_usage_in_manual_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """record_usage works with manually started tasks (US-009 + US-011)."""
        tracker.register_rate(service="search-api.com", per="query", cost_usd="0.002")

        task = tracker.start_task(task_type="manual_usage")
        event = task.record_usage(service="search-api.com", units=5)
        task.end()

        assert event.cost_usd == Decimal("0.010")
        assert event.pricing_source == "rate_registry"
        assert event.pricing_version is not None

        tasks = storage.query_tasks(task_type="manual_usage")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.010")
        assert t.total_cost_usd == Decimal("0.010")

    def test_record_usage_fractional_units(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Fractional units are supported (e.g. 0.5 requests)."""
        tracker.register_rate(service="compute", per="hour", cost_usd="1.00")

        with tracker.task(task_type="usage_frac") as task:
            event = task.record_usage(service="compute", units=0.5)

        assert event.cost_usd == Decimal("0.50") or event.cost_usd == Decimal("0.5")

    def test_record_usage_and_record_cost_in_same_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Both record_usage and record_cost can be used in the same task."""
        tracker.register_rate(service="maps", per="request", cost_usd="0.005")

        with tracker.task(task_type="mixed_usage") as task:
            task.record_usage(service="maps", units=2)
            task.record_cost(service="one_off_api", cost_usd="0.10")

        tasks = storage.query_tasks(task_type="mixed_usage")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.110")
        assert t.total_cost_usd == Decimal("0.110")


# ---------------------------------------------------------------------------
# End-to-end integration test
# ---------------------------------------------------------------------------


class TestRateRegistryIntegration:
    """Full workflow: register rates, track task, verify costs."""

    def test_full_workflow(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Register rates → track task with usage → verify aggregation."""
        tracker.register_rate(service="maps.googleapis.com", per="request", cost_usd="0.005")
        tracker.register_rate(service="ocr-api.com", per="page", cost_usd="0.01")

        with tracker.task(
            task_type="full_workflow",
            customer_id="acme",
            project_id="proj-1",
        ) as task:
            task.record_usage(service="maps.googleapis.com")  # 1 * 0.005
            task.record_usage(service="ocr-api.com", units=3)  # 3 * 0.01

        tasks = storage.query_tasks(task_type="full_workflow")
        t = tasks[0]
        assert t.status == "success"
        assert t.external_cost_usd == Decimal("0.035")
        assert t.total_cost_usd == Decimal("0.035")
        assert t.customer_id == "acme"

        # Verify events
        events = storage.query_events(task_id=str(t.task_id))
        assert len(events) == 2
        costs = sorted([e.cost_usd for e in events])
        assert costs == [Decimal("0.005"), Decimal("0.030")]

    def test_load_rates_then_track(
        self, tracker: CostTracker, storage: SQLiteStorage, tmp_path: Path
    ) -> None:
        """Load rates from YAML → track task → verify costs."""
        yaml_content = """\
rates:
  maps.googleapis.com:
    per: request
    cost_usd: "0.005"
  ocr-api.com:
    per: page
    cost_usd: "0.01"
"""
        yaml_file = tmp_path / "rates.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")
        tracker.load_rates(yaml_file)

        with tracker.task(task_type="yaml_workflow") as task:
            task.record_usage(service="maps.googleapis.com")
            task.record_usage(service="ocr-api.com", units=3)

        tasks = storage.query_tasks(task_type="yaml_workflow")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.035")
        assert t.total_cost_usd == Decimal("0.035")

    def test_export_then_load_rates(
        self, tracker: CostTracker, storage: SQLiteStorage, tmp_path: Path
    ) -> None:
        """Register → export → new tracker loads → verify same rates."""
        tracker.register_rate(service="svc-a", per="request", cost_usd="0.005")
        tracker.register_rate(service="svc-b", per="page", cost_usd="0.01")

        yaml_file = tmp_path / "rates.yaml"
        tracker.export_rates(yaml_file)

        # New tracker with fresh storage
        storage2 = SQLiteStorage(db_path=tmp_path / "test2.db")
        tracker2 = CostTracker(storage=storage2, auto_instrument=[])
        tracker2.load_rates(yaml_file)

        with tracker2.task(task_type="loaded_workflow") as task:
            task.record_usage(service="svc-a", units=2)
            task.record_usage(service="svc-b", units=1)

        tasks = storage2.query_tasks(task_type="loaded_workflow")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.020")
        assert t.total_cost_usd == Decimal("0.020")
        storage2.close()


# ---------------------------------------------------------------------------
# Public API export tests
# ---------------------------------------------------------------------------


class TestPublicAPIExports:
    """RateEntry and RateRegistry are accessible from the top-level package."""

    def test_rate_entry_exported(self) -> None:
        import dexcost

        assert dexcost.RateEntry is RateEntry

    def test_rate_registry_exported(self) -> None:
        import dexcost

        assert dexcost.RateRegistry is RateRegistry
