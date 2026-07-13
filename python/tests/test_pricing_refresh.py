"""Tests for centralized pricing data refresh on init() (US-044).

Verifies that PricingEngine.refresh_from_server() fetches data from the
Control Layer and updates the model map, and that failure is silent.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from dexcost.pricing import PricingEngine

CONTROL_PLANE_PRICING_RESPONSE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pricing_refresh"
    / "control_plane_latest.json"
).read_text(encoding="utf-8")


class TestPricingRefreshFromServer:
    """Tests for PricingEngine.refresh_from_server."""

    def test_refresh_updates_model_map(self) -> None:
        """Successful refresh replaces the model map with server data."""
        engine = PricingEngine(api_key="dx_test_refresh")

        fake_response = MagicMock()
        fake_response.read.return_value = CONTROL_PLANE_PRICING_RESPONSE.encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
            engine.refresh_from_server("http://localhost:3000")

        request = urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer dx_test_refresh"
        assert engine.pricing_version == "server-v-42"

        # The engine should now know about the new model
        result = engine.get_cost("new-model-v1", input_tokens=1000, output_tokens=500)
        assert result.cost_usd > Decimal("0")
        assert result.pricing_source == "litellm"

    def test_refresh_failure_is_silent(self) -> None:
        """Network failure during refresh does not raise; engine keeps bundled data."""
        engine = PricingEngine()

        with patch("urllib.request.urlopen", side_effect=ConnectionError("no server")):
            # Should not raise
            engine.refresh_from_server("http://nonexistent:9999")

        # Bundled data is still usable
        result = engine.get_cost("gpt-4o", input_tokens=100, output_tokens=50)
        assert result.cost_confidence in ("computed", "unknown")

    def test_refresh_with_invalid_json_is_silent(self) -> None:
        """Malformed JSON from server does not crash the engine."""
        engine = PricingEngine()

        fake_response = MagicMock()
        fake_response.read.return_value = b"not json at all"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response):
            engine.refresh_from_server("http://localhost:3000")

        # Engine should still function with bundled data
        result = engine.get_cost("gpt-4o", input_tokens=100, output_tokens=50)
        assert result.cost_confidence in ("computed", "unknown")

    def test_empty_model_map_keeps_existing_catalog(self) -> None:
        engine = PricingEngine()
        original_version = engine.pricing_version
        original_count = engine.model_count

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"data": {"data": {}, "pricing_version": "empty-v1"}}
        ).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response):
            engine.refresh_from_server("http://localhost:3000")

        assert engine.pricing_version == original_version
        assert engine.model_count == original_count

    def test_set_api_key_updates_refresh_auth(self) -> None:
        engine = PricingEngine(api_key="dx_test_old")
        engine.set_api_key("dx_test_new")

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {
                "data": {
                    "data": {
                        "new-model": {
                            "input_cost_per_token": 0.00001,
                            "output_cost_per_token": 0.00002,
                        }
                    },
                    "pricing_version": "new-key-v1",
                }
            }
        ).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
            engine.refresh_from_server("http://localhost:3000")

        request = urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer dx_test_new"

    def test_refresh_is_non_blocking(self) -> None:
        """start_background_refresh launches a daemon thread and returns immediately."""
        engine = PricingEngine()

        with patch("urllib.request.urlopen", side_effect=ConnectionError("no server")):
            # Should return immediately, not block
            engine.start_background_refresh("http://localhost:3000")

        # Give the thread a moment to complete (it will fail silently)
        time.sleep(0.1)

        # Engine still works
        result = engine.get_cost("gpt-4o", input_tokens=100, output_tokens=50)
        assert result.cost_confidence in ("computed", "unknown")
        engine.close()

    def test_background_refresh_repeats_until_closed(self) -> None:
        engine = PricingEngine()
        fake_response = MagicMock()
        fake_response.read.return_value = CONTROL_PLANE_PRICING_RESPONSE.encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
            engine.start_background_refresh("http://localhost:3000", interval_seconds=0.02)
            deadline = time.monotonic() + 1
            while urlopen.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            engine.close()

        assert urlopen.call_count >= 2
