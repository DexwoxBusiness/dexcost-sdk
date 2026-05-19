"""Tests for the AWS Lambda cost adapter (US-043).

Verifies lambda_cost() returns correct Decimal costs for various
region/memory/duration combinations, matching AWS calculator values.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.adapters.aws_lambda import lambda_cost, get_supported_regions


class TestLambdaCost:
    """Tests for the lambda_cost pure function."""

    def test_basic_us_east_1(self) -> None:
        """1 second, 128 MB in us-east-1 matches AWS calculator."""
        result = lambda_cost(duration_ms=1000, memory_mb=128, region="us-east-1")
        # 128 MB = 0.125 GB. 1s * 0.125 GB * 0.0000166667/GB-s = 0.0000020833
        # + request charge 0.0000002 = 0.0000022833
        assert result["cost_usd"] == Decimal("0.0000022833375")
        assert result["details"]["region"] == "us-east-1"
        assert result["details"]["duration_ms"] == 1000
        assert result["details"]["memory_mb"] == 128
        assert result["details"]["gb_seconds"] == Decimal("0.125")

    def test_higher_memory(self) -> None:
        """512 MB for 3 seconds in us-east-1."""
        result = lambda_cost(duration_ms=3000, memory_mb=512, region="us-east-1")
        # 512 MB = 0.5 GB. 3s * 0.5 GB = 1.5 GB-s * 0.0000166667 = 0.0000250001
        # + request 0.0000002 = 0.0000252001
        expected_duration_cost = Decimal("1.5") * Decimal("0.0000166667")
        expected_total = expected_duration_cost + Decimal("0.0000002")
        assert result["cost_usd"] == expected_total

    def test_eu_central_1_pricing(self) -> None:
        """EU regions use higher per-GB-second rate."""
        result = lambda_cost(duration_ms=1000, memory_mb=1024, region="eu-central-1")
        # 1024 MB = 1 GB. 1s * 1 GB * 0.0000175000 = 0.0000175000
        # + request 0.0000002 = 0.0000177000
        assert result["cost_usd"] == Decimal("0.0000177000")

    def test_cost_usd_is_decimal(self) -> None:
        """cost_usd must be a Decimal, never a float."""
        result = lambda_cost(duration_ms=500, memory_mb=256, region="us-west-2")
        assert isinstance(result["cost_usd"], Decimal)

    def test_zero_duration_returns_request_charge_only(self) -> None:
        """0 ms duration still incurs the per-request charge."""
        result = lambda_cost(duration_ms=0, memory_mb=128, region="us-east-1")
        assert result["cost_usd"] == Decimal("0.0000002")

    def test_unknown_region_raises_value_error(self) -> None:
        """An unknown region should raise ValueError with helpful message."""
        with pytest.raises(ValueError, match="Unknown AWS region"):
            lambda_cost(duration_ms=1000, memory_mb=128, region="mars-west-1")

    def test_negative_duration_raises_value_error(self) -> None:
        """Negative duration is invalid."""
        with pytest.raises(ValueError, match="duration_ms must be >= 0"):
            lambda_cost(duration_ms=-100, memory_mb=128, region="us-east-1")

    def test_zero_memory_raises_value_error(self) -> None:
        """Zero memory is invalid."""
        with pytest.raises(ValueError, match="memory_mb must be > 0"):
            lambda_cost(duration_ms=1000, memory_mb=0, region="us-east-1")

    def test_details_includes_pricing_breakdown(self) -> None:
        """Details dict includes duration_cost, request_cost, rate info."""
        result = lambda_cost(duration_ms=2000, memory_mb=256, region="us-east-1")
        details = result["details"]
        assert "duration_cost_usd" in details
        assert "request_cost_usd" in details
        assert "rate_per_gb_second" in details
        assert details["request_cost_usd"] == Decimal("0.0000002")

    def test_get_supported_regions(self) -> None:
        """get_supported_regions returns a non-empty list of region strings."""
        regions = get_supported_regions()
        assert isinstance(regions, list)
        assert len(regions) >= 10
        assert "us-east-1" in regions
        assert "eu-central-1" in regions

    def test_sub_millisecond_rounds_up_to_1ms(self) -> None:
        """AWS Lambda rounds up to the nearest 1ms. 0 ms stays 0."""
        # Duration 1 ms, 128 MB
        result = lambda_cost(duration_ms=1, memory_mb=128, region="us-east-1")
        assert result["cost_usd"] > Decimal("0.0000002")  # More than just request charge

    def test_return_type_is_dict(self) -> None:
        """Return value is a plain dict with cost_usd and details keys."""
        result = lambda_cost(duration_ms=100, memory_mb=128, region="us-east-1")
        assert isinstance(result, dict)
        assert "cost_usd" in result
        assert "details" in result
