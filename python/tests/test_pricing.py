"""Tests for the LLM pricing engine (US-010)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dexcost.pricing import CostResult, PricingEngine, _compute_hash

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pricing(tmp_path: Path) -> PricingEngine:
    """A PricingEngine loaded from the bundled data with auto-update disabled."""
    engine = PricingEngine(auto_update=False)
    yield engine
    engine.close()


@pytest.fixture()
def minimal_data(tmp_path: Path) -> Path:
    """Write a small, deterministic pricing JSON to *tmp_path* and return the path."""
    data: dict[str, Any] = {
        "sample_spec": {"input_cost_per_token": 0},
        "gpt-4o": {
            "input_cost_per_token": 0.0000025,
            "output_cost_per_token": 0.00001,
            "cache_read_input_token_cost": 0.00000125,
            "litellm_provider": "openai",
            "max_input_tokens": 128000,
            "max_output_tokens": 16384,
            "mode": "chat",
        },
        "gpt-4o-2024-08-06": {
            "input_cost_per_token": 0.0000025,
            "output_cost_per_token": 0.00001,
            "cache_read_input_token_cost": 0.00000125,
            "litellm_provider": "openai",
            "max_input_tokens": 128000,
            "max_output_tokens": 16384,
            "mode": "chat",
        },
        "claude-3-5-sonnet-20241022": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_token_cost": 0.0000003,
            "litellm_provider": "anthropic",
        },
        "gpt-3.5-turbo": {
            "input_cost_per_token": 0.0000005,
            "output_cost_per_token": 0.0000015,
            "litellm_provider": "openai",
        },
    }
    path = tmp_path / "model_cost_map.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture()
def engine(minimal_data: Path) -> PricingEngine:
    """PricingEngine using the deterministic minimal data."""
    eng = PricingEngine(data_path=minimal_data, auto_update=False)
    yield eng
    eng.close()


# ---------------------------------------------------------------------------
# CostResult basics
# ---------------------------------------------------------------------------


class TestCostResult:
    def test_fields(self) -> None:
        r = CostResult(
            cost_usd=Decimal("0.01"),
            cost_confidence="computed",
            pricing_source="litellm",
            pricing_version="abc123",
        )
        assert r.cost_usd == Decimal("0.01")
        assert r.cost_confidence == "computed"
        assert r.pricing_source == "litellm"
        assert r.pricing_version == "abc123"

    def test_frozen(self) -> None:
        r = CostResult(
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
            pricing_version="x",
        )
        with pytest.raises(AttributeError):
            r.cost_usd = Decimal("1")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Known models
# ---------------------------------------------------------------------------


class TestKnownModels:
    """AC: get_cost returns correct cost_usd for known models."""

    def test_gpt4o_basic(self, engine: PricingEngine) -> None:
        result = engine.get_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        # input: 1000 * 0.0000025 = 0.0025
        # output: 500 * 0.00001 = 0.005
        # total: 0.0075
        assert result.cost_usd == Decimal("0.0075")
        assert result.cost_confidence == "computed"
        assert result.pricing_source == "litellm"
        assert result.pricing_version  # non-empty hash

    def test_gpt4o_dated_variant(self, engine: PricingEngine) -> None:
        """AC: 'gpt-4o-2024-08-06' resolves correctly."""
        result = engine.get_cost("gpt-4o-2024-08-06", input_tokens=1000, output_tokens=500)
        assert result.cost_usd == Decimal("0.0075")
        assert result.pricing_source == "litellm"

    def test_claude_sonnet(self, engine: PricingEngine) -> None:
        result = engine.get_cost(
            "claude-3-5-sonnet-20241022", input_tokens=2000, output_tokens=1000
        )
        # input: 2000 * 0.000003 = 0.006
        # output: 1000 * 0.000015 = 0.015
        assert result.cost_usd == Decimal("0.021")

    def test_gpt35_turbo(self, engine: PricingEngine) -> None:
        result = engine.get_cost("gpt-3.5-turbo", input_tokens=10000, output_tokens=2000)
        # input: 10000 * 0.0000005 = 0.005
        # output: 2000 * 0.0000015 = 0.003
        assert result.cost_usd == Decimal("0.008")

    def test_zero_tokens(self, engine: PricingEngine) -> None:
        result = engine.get_cost("gpt-4o", input_tokens=0, output_tokens=0)
        assert result.cost_usd == Decimal("0")
        assert result.cost_confidence == "computed"


# ---------------------------------------------------------------------------
# Unknown models
# ---------------------------------------------------------------------------


class TestUnknownModels:
    """AC: unknown model → cost_usd=0, cost_confidence='unknown', warning logged."""

    def test_unknown_returns_zero(self, engine: PricingEngine) -> None:
        result = engine.get_cost("nonexistent-model-xyz", input_tokens=1000, output_tokens=500)
        assert result.cost_usd == Decimal("0")
        assert result.cost_confidence == "unknown"
        assert result.pricing_source == "unknown"

    def test_unknown_logs_warning(self, engine: PricingEngine, caplog: Any) -> None:
        with caplog.at_level(logging.WARNING, logger="dexcost.pricing"):
            engine.get_cost("nonexistent-model-xyz", 100, 50)
        assert "not found in pricing data" in caplog.text

    def test_unknown_still_has_pricing_version(self, engine: PricingEngine) -> None:
        result = engine.get_cost("nonexistent-model-xyz", 100, 50)
        assert result.pricing_version  # non-empty


# ---------------------------------------------------------------------------
# Model alias resolution
# ---------------------------------------------------------------------------


class TestModelAliases:
    """AC: model aliases resolve correctly."""

    def test_exact_match_preferred(self, engine: PricingEngine) -> None:
        # Both "gpt-4o" and "gpt-4o-2024-08-06" exist; exact match wins
        r1 = engine.get_cost("gpt-4o", 1000, 500)
        r2 = engine.get_cost("gpt-4o-2024-08-06", 1000, 500)
        assert r1.pricing_source == "litellm"
        assert r2.pricing_source == "litellm"

    def test_provider_prefix_stripped(self, engine: PricingEngine) -> None:
        """'openai/gpt-4o' should resolve to 'gpt-4o'."""
        result = engine.get_cost("openai/gpt-4o", 1000, 500)
        assert result.cost_usd == Decimal("0.0075")
        assert result.pricing_source == "litellm"

    def test_date_suffix_fallback(self, engine: PricingEngine) -> None:
        """'gpt-4o-2099-01-01' (unknown dated variant) falls back to 'gpt-4o'."""
        result = engine.get_cost("gpt-4o-2099-01-01", 1000, 500)
        assert result.cost_usd == Decimal("0.0075")
        assert result.pricing_source == "litellm"

    def test_bundled_data_aliases(self, pricing: PricingEngine) -> None:
        """Verify alias resolution against the full bundled dataset."""
        r1 = pricing.get_cost("gpt-4o", 1000, 500)
        r2 = pricing.get_cost("gpt-4o-2024-08-06", 1000, 500)
        # Both should resolve and have the same cost
        assert r1.pricing_source == "litellm"
        assert r2.pricing_source == "litellm"
        assert r1.cost_usd > Decimal("0")
        assert r2.cost_usd > Decimal("0")


# ---------------------------------------------------------------------------
# Cached token discount
# ---------------------------------------------------------------------------


class TestCachedTokens:
    """AC: cached token discount calculation."""

    def test_cached_tokens_discount(self, engine: PricingEngine) -> None:
        """Cached tokens are charged at cache rate, not full input rate."""
        # 1000 input, 500 output, 400 cached
        result = engine.get_cost("gpt-4o", input_tokens=1000, output_tokens=500, cached_tokens=400)
        # non-cached input: (1000 - 400) * 0.0000025 = 0.0015
        # cached: 400 * 0.00000125 = 0.0005
        # output: 500 * 0.00001 = 0.005
        # total: 0.007
        assert result.cost_usd == Decimal("0.007")

    def test_all_cached(self, engine: PricingEngine) -> None:
        """All input tokens are cached."""
        result = engine.get_cost("gpt-4o", input_tokens=1000, output_tokens=0, cached_tokens=1000)
        # cached: 1000 * 0.00000125 = 0.00125
        assert result.cost_usd == Decimal("0.00125")

    def test_cached_exceeds_input_clamped(self, engine: PricingEngine) -> None:
        """cached_tokens > input_tokens is clamped."""
        result = engine.get_cost("gpt-4o", input_tokens=100, output_tokens=0, cached_tokens=500)
        # Clamped to 100 cached: 100 * 0.00000125 = 0.000125
        assert result.cost_usd == Decimal("0.000125")

    def test_no_cache_rate_model(self, engine: PricingEngine) -> None:
        """Model without cache_read_input_token_cost: cached tokens charged at 0."""
        result = engine.get_cost(
            "gpt-3.5-turbo", input_tokens=1000, output_tokens=500, cached_tokens=200
        )
        # non-cached: 800 * 0.0000005 = 0.0004
        # cached: 200 * 0 = 0 (no cache_read_input_token_cost in data)
        # output: 500 * 0.0000015 = 0.00075
        # total: 0.00115
        assert result.cost_usd == Decimal("0.00115")

    def test_zero_cached_same_as_no_arg(self, engine: PricingEngine) -> None:
        r1 = engine.get_cost("gpt-4o", 1000, 500, cached_tokens=0)
        r2 = engine.get_cost("gpt-4o", 1000, 500)
        assert r1.cost_usd == r2.cost_usd


# ---------------------------------------------------------------------------
# Custom pricing
# ---------------------------------------------------------------------------


class TestCustomPricing:
    """AC: custom pricing takes precedence over bundled data."""

    def test_set_and_get_custom(self, engine: PricingEngine) -> None:
        engine.set_custom_pricing("my-fine-tune", input_per_1k=0.005, output_per_1k=0.015)
        result = engine.get_cost("my-fine-tune", input_tokens=2000, output_tokens=1000)
        # input: 2000/1000 * 0.005 = 0.01
        # output: 1000/1000 * 0.015 = 0.015
        assert result.cost_usd == Decimal("0.025")
        assert result.pricing_source == "custom"
        assert result.cost_confidence == "computed"

    def test_custom_overrides_bundled(self, engine: PricingEngine) -> None:
        """Custom pricing for an existing model overrides bundled data."""
        engine.set_custom_pricing("gpt-4o", input_per_1k="0.001", output_per_1k="0.002")
        result = engine.get_cost("gpt-4o", input_tokens=1000, output_tokens=1000)
        # input: 1 * 0.001 = 0.001
        # output: 1 * 0.002 = 0.002
        assert result.cost_usd == Decimal("0.003")
        assert result.pricing_source == "custom"

    def test_custom_decimal_precision(self, engine: PricingEngine) -> None:
        engine.set_custom_pricing(
            "precise-model",
            input_per_1k=Decimal("0.00123456"),
            output_per_1k=Decimal("0.00654321"),
        )
        result = engine.get_cost("precise-model", input_tokens=1000, output_tokens=1000)
        expected = Decimal("0.00123456") + Decimal("0.00654321")
        assert result.cost_usd == expected

    def test_custom_ignores_cached_tokens(self, engine: PricingEngine) -> None:
        """Custom pricing doesn't apply cache discounts (simple per-1k model)."""
        engine.set_custom_pricing("my-model", input_per_1k=0.01, output_per_1k=0.02)
        r1 = engine.get_cost("my-model", 1000, 500, cached_tokens=0)
        r2 = engine.get_cost("my-model", 1000, 500, cached_tokens=500)
        # Custom pricing uses total input_tokens regardless of cached
        assert r1.cost_usd == r2.cost_usd


# ---------------------------------------------------------------------------
# Pricing source and version
# ---------------------------------------------------------------------------


class TestPricingMetadata:
    """AC: pricing_source and pricing_version set correctly."""

    def test_litellm_source(self, engine: PricingEngine) -> None:
        result = engine.get_cost("gpt-4o", 1000, 500)
        assert result.pricing_source == "litellm"

    def test_custom_source(self, engine: PricingEngine) -> None:
        engine.set_custom_pricing("x", input_per_1k=0.1, output_per_1k=0.2)
        result = engine.get_cost("x", 1000, 500)
        assert result.pricing_source == "custom"

    def test_unknown_source(self, engine: PricingEngine) -> None:
        result = engine.get_cost("nonexistent", 1000, 500)
        assert result.pricing_source == "unknown"

    def test_pricing_version_is_hash(self, engine: PricingEngine) -> None:
        result = engine.get_cost("gpt-4o", 1000, 500)
        assert len(result.pricing_version) == 12
        assert all(c in "0123456789abcdef" for c in result.pricing_version)

    def test_pricing_version_stable(self, engine: PricingEngine) -> None:
        r1 = engine.get_cost("gpt-4o", 1000, 500)
        r2 = engine.get_cost("gpt-4o", 2000, 1000)
        assert r1.pricing_version == r2.pricing_version

    def test_pricing_version_property(self, engine: PricingEngine) -> None:
        assert engine.pricing_version == engine.get_cost("gpt-4o", 0, 0).pricing_version


# ---------------------------------------------------------------------------
# Background update
# ---------------------------------------------------------------------------


class TestBackgroundUpdate:
    """AC: background pricing update (non-blocking, fail-silent)."""

    def test_auto_update_defaults_to_false(self, minimal_data: Path) -> None:
        """PRD: no background pricing update in v1.0. Default must be False."""
        eng = PricingEngine(data_path=minimal_data)
        assert eng._update_timer is None
        eng.close()

    def test_auto_update_disabled(self, engine: PricingEngine) -> None:
        # engine fixture has auto_update=False
        assert engine._update_timer is None

    def test_auto_update_enabled(self, tmp_path: Path, minimal_data: Path) -> None:
        eng = PricingEngine(data_path=minimal_data, auto_update=True)
        assert eng._update_timer is not None
        assert eng._update_timer.daemon is True
        eng.close()
        assert eng._update_timer is None

    def test_failed_update_logs_warning(self, engine: PricingEngine, caplog: Any) -> None:
        """A failed HTTP fetch should log a warning and not crash."""
        with (
            patch("urllib.request.urlopen", side_effect=OSError("network down")),
            caplog.at_level(logging.WARNING, logger="dexcost.pricing"),
        ):
            engine._background_update()
        assert "Background pricing update failed" in caplog.text

    def test_successful_update_changes_version(
        self, engine: PricingEngine, minimal_data: Path
    ) -> None:
        old_version = engine.pricing_version
        new_data = json.dumps(
            {
                "gpt-4o": {
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00002,
                }
            }
        )
        mock_resp = type(
            "Resp",
            (),
            {
                "read": lambda self: new_data.encode("utf-8"),
                "__enter__": lambda self: self,
                "__exit__": lambda self, *a: None,
            },
        )()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            engine._background_update()
        assert engine.pricing_version != old_version


# ---------------------------------------------------------------------------
# Integration with CostTracker
# ---------------------------------------------------------------------------


class TestCostTrackerIntegration:
    """US-010 integration: pricing engine is used from CostTracker."""

    def test_tracker_has_pricing(self, tmp_path: Path) -> None:
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        assert tracker.pricing is not None
        tracker.pricing.close()

    def test_tracker_set_custom_pricing(self, tmp_path: Path) -> None:
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        tracker.set_custom_pricing("my-model", input_per_1k=0.01, output_per_1k=0.02)
        result = tracker.get_cost("my-model", 1000, 500)
        assert result.cost_usd == Decimal("0.02")
        assert result.pricing_source == "custom"
        tracker.pricing.close()

    def test_tracker_get_cost(self, tmp_path: Path) -> None:
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        result = tracker.get_cost("gpt-4o", 1000, 500)
        assert result.cost_usd > Decimal("0")
        assert result.pricing_source == "litellm"
        tracker.pricing.close()

    def test_record_llm_call_auto_cost(self, tmp_path: Path) -> None:
        """record_llm_call without cost_usd auto-computes via pricing engine."""
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        with tracker.task(task_type="test") as task:
            event = task.record_llm_call(
                provider="openai",
                model="gpt-4o",
                input_tokens=1000,
                output_tokens=500,
            )
        assert event.cost_usd > Decimal("0")
        assert event.cost_confidence == "computed"
        assert event.pricing_source == "litellm"
        assert event.pricing_version is not None
        tracker.pricing.close()

    def test_record_llm_call_manual_cost(self, tmp_path: Path) -> None:
        """record_llm_call with explicit cost_usd uses manual pricing."""
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        with tracker.task(task_type="test") as task:
            event = task.record_llm_call(
                provider="openai",
                model="gpt-4o",
                input_tokens=1000,
                output_tokens=500,
                cost_usd="0.05",
            )
        assert event.cost_usd == Decimal("0.05")
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "manual"
        tracker.pricing.close()

    def test_record_llm_call_unknown_model(self, tmp_path: Path) -> None:
        """Unknown model auto-computes to cost_usd=0, cost_confidence='unknown'."""
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        with tracker.task(task_type="test") as task:
            event = task.record_llm_call(
                provider="unknown",
                model="nonexistent-model-xyz",
                input_tokens=1000,
                output_tokens=500,
            )
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.pricing_source == "unknown"
        tracker.pricing.close()

    def test_record_llm_call_custom_pricing(self, tmp_path: Path) -> None:
        """Custom pricing is used when set via tracker."""
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.tracker import CostTracker

        db = tmp_path / "costs.db"
        tracker = CostTracker(
            storage=SQLiteStorage(str(db)),
            auto_update_pricing=False,
            auto_instrument=[],
        )
        tracker.set_custom_pricing("my-fine-tune", input_per_1k=0.005, output_per_1k=0.015)
        with tracker.task(task_type="test") as task:
            event = task.record_llm_call(
                provider="openai",
                model="my-fine-tune",
                input_tokens=2000,
                output_tokens=1000,
            )
        assert event.cost_usd == Decimal("0.025")
        assert event.pricing_source == "custom"
        tracker.pricing.close()


# ---------------------------------------------------------------------------
# Bundled data integrity
# ---------------------------------------------------------------------------


class TestBundledData:
    """AC: model_cost_map.json is bundled and has 400+ models."""

    def test_bundled_data_loads(self) -> None:
        engine = PricingEngine(auto_update=False)
        assert len(engine._model_map) > 400
        engine.close()

    def test_sample_spec_removed(self) -> None:
        engine = PricingEngine(auto_update=False)
        assert "sample_spec" not in engine._model_map
        engine.close()

    def test_known_models_present(self) -> None:
        engine = PricingEngine(auto_update=False)
        for model in ["gpt-4o", "gpt-4", "gpt-3.5-turbo", "claude-3-5-sonnet-20241022"]:
            assert model in engine._model_map, f"{model} missing from bundled data"
        engine.close()


# ---------------------------------------------------------------------------
# Public API exports
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_pricing_engine_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "PricingEngine")

    def test_cost_result_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "CostResult")

    def test_pricing_source_has_custom_and_unknown(self) -> None:
        from dexcost.models.enums import PricingSource

        assert PricingSource.CUSTOM == "custom"
        assert PricingSource.UNKNOWN == "unknown"


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


class TestComputeHash:
    def test_deterministic(self) -> None:
        h1 = _compute_hash("hello world")
        h2 = _compute_hash("hello world")
        assert h1 == h2

    def test_different_input_different_hash(self) -> None:
        h1 = _compute_hash("hello")
        h2 = _compute_hash("world")
        assert h1 != h2

    def test_12_char_hex(self) -> None:
        h = _compute_hash("test")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)
