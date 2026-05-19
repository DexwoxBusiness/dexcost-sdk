"""Tests for centralized pricing data refresh on init() (US-044).

Verifies that PricingEngine.refresh_from_server() fetches data from the
Control Layer and updates the model map, and that failure is silent.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dexcost.pricing import PricingEngine


class TestPricingRefreshFromServer:
    """Tests for PricingEngine.refresh_from_server."""

    def test_refresh_updates_model_map(self) -> None:
        """Successful refresh replaces the model map with server data."""
        engine = PricingEngine()
        original_version = engine.pricing_version

        fake_server_data = {
            "gpt-4o": {
                "input_cost_per_token": 0.000005,
                "output_cost_per_token": 0.000015,
            },
            "new-model-from-server": {
                "input_cost_per_token": 0.00001,
                "output_cost_per_token": 0.00003,
            },
        }

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"data": {"data": fake_server_data, "pricing_version": "server123"}}
        ).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_response):
            engine.refresh_from_server("http://localhost:3000")

        # The engine should now know about the new model
        result = engine.get_cost("new-model-from-server", input_tokens=1000, output_tokens=500)
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

    def test_refresh_is_non_blocking(self) -> None:
        """start_background_refresh launches a daemon thread and returns immediately."""
        engine = PricingEngine()

        with patch("urllib.request.urlopen", side_effect=ConnectionError("no server")):
            # Should return immediately, not block
            engine.start_background_refresh("http://localhost:3000")

        # Give the thread a moment to complete (it will fail silently)
        import time
        time.sleep(0.1)

        # Engine still works
        result = engine.get_cost("gpt-4o", input_tokens=100, output_tokens=50)
        assert result.cost_confidence in ("computed", "unknown")
