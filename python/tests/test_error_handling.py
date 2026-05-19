"""Tests for error handling and graceful degradation.

Verifies that the SDK never crashes on corrupt data, missing files,
or malformed inputs.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.service_catalog import ServiceCatalog
from dexcost.pricing import PricingEngine
from dexcost.rates import RateRegistry
from dexcost.redaction import enforce_metadata_limit


class TestServiceCatalogErrorHandling:
    """ServiceCatalog gracefully handles corrupt or missing data."""

    def test_missing_catalog_file_loads_empty(self, tmp_path: Path) -> None:
        catalog = ServiceCatalog(data_path=tmp_path / "nonexistent.json")
        assert len(catalog.entries) == 0

    def test_corrupt_json_loads_empty(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json", encoding="utf-8")
        catalog = ServiceCatalog(data_path=bad_file)
        assert len(catalog.entries) == 0

    def test_extract_cost_non_numeric_body_returns_none(self) -> None:
        catalog = ServiceCatalog()
        entry = catalog.lookup("https://api.tavily.com/search")
        if entry is None:
            pytest.skip("Tavily not in catalog")
        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"usage": {"credits": "not_a_number"}},
        )
        assert result is None

    def test_extract_cost_non_numeric_header_returns_none(self) -> None:
        catalog = ServiceCatalog()
        entry = catalog.lookup("https://app.scrapingbee.com/api/v1")
        if entry is None:
            pytest.skip("ScrapingBee not in catalog")
        result = catalog.extract_cost(
            entry,
            response_headers={"Spb-cost": "not_a_number"},
            response_body=None,
        )
        assert result is None


class TestModelDeserializationErrors:
    """Event and Task from_dict handle malformed data gracefully."""

    def test_event_from_dict_missing_fields(self) -> None:
        with pytest.raises(ValueError, match="Invalid event data"):
            Event.from_dict({})

    def test_event_from_dict_bad_uuid(self) -> None:
        with pytest.raises(ValueError, match="Invalid event data"):
            Event.from_dict({"event_id": "not-a-uuid", "task_id": "x", "event_type": "llm_call", "occurred_at": "2026-01-01T00:00:00", "cost_usd": "0.01", "cost_confidence": "exact"})

    def test_event_from_dict_bad_decimal(self) -> None:
        with pytest.raises(ValueError, match="Invalid event data"):
            Event.from_dict({"event_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()), "event_type": "llm_call", "occurred_at": "2026-01-01T00:00:00", "cost_usd": "not_a_number", "cost_confidence": "exact"})

    def test_task_from_dict_missing_fields(self) -> None:
        with pytest.raises(ValueError, match="Invalid task data"):
            Task.from_dict({})


class TestSQLiteErrorHandling:
    """SQLite storage handles corrupt data in rows."""

    def test_json_loads_handles_corrupt_json(self) -> None:
        from dexcost.storage.sqlite import _json_loads
        assert _json_loads(None) == {}
        assert _json_loads("{}") == {}
        assert _json_loads("{bad json") == {}


class TestRateRegistryErrorHandling:
    """RateRegistry handles missing/corrupt files."""

    def test_load_missing_file_raises_valueerror(self, tmp_path: Path) -> None:
        registry = RateRegistry()
        with pytest.raises(ValueError, match="Cannot read"):
            registry.load(str(tmp_path / "nonexistent.yaml"))

    def test_export_to_readonly_raises_valueerror(self, tmp_path: Path) -> None:
        registry = RateRegistry()
        registry.register("test", "request", Decimal("0.01"))
        bad_path = str(tmp_path / "readonly" / "rates.yaml")
        # Don't create the directory -- write should fail
        with pytest.raises(ValueError, match="Cannot write"):
            registry.export(bad_path)


class TestRedactionErrorHandling:
    """Redaction handles unserializable data."""

    def test_enforce_metadata_limit_circular_reference(self) -> None:
        d: dict[str, Any] = {"key": "value"}
        d["self"] = d  # circular reference
        result = enforce_metadata_limit(d)
        assert "_truncated" in result or "_error" in result


class TestPricingErrorHandling:
    """PricingEngine handles corrupt data."""

    def test_set_custom_pricing_invalid_values(self) -> None:
        engine = PricingEngine()
        with pytest.raises(ValueError, match="Invalid pricing"):
            engine.set_custom_pricing("model", "not_a_number", "also_bad")  # type: ignore
