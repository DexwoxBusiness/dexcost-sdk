"""AWS Lambda cost adapter — compute cost from duration, memory, and region.

Pure function ``lambda_cost`` returns a dict with ``cost_usd`` (Decimal) and
``details`` breakdown.  Uses bundled pricing JSON; no network I/O.

Implements US-043.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from importlib import resources
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bundled pricing data (loaded once at import time)
# ---------------------------------------------------------------------------

_pricing_data: dict[str, Any] | None = None


def _load_pricing() -> dict[str, Any]:
    """Load and cache the bundled AWS Lambda pricing JSON."""
    global _pricing_data
    if _pricing_data is not None:
        return _pricing_data

    ref = (
        resources.files("dexcost")
        .joinpath("adapters")
        .joinpath("data")
        .joinpath("aws_lambda_pricing.json")
    )
    raw = ref.read_text(encoding="utf-8")
    try:
        _pricing_data = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Failed to parse AWS pricing data")
        _pricing_data = {}
    return _pricing_data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_supported_regions() -> list[str]:
    """Return a sorted list of AWS region codes with bundled pricing data."""
    data = _load_pricing()
    return sorted(data["regions"].keys())


def lambda_cost(
    duration_ms: int,
    memory_mb: int,
    region: str,
) -> dict[str, Any]:
    """Calculate the cost of a single AWS Lambda invocation.

    This is a **pure function** — no I/O, no side effects.  It uses the
    bundled ``aws_lambda_pricing.json`` for rates.

    Args:
        duration_ms: Execution duration in milliseconds (>= 0).
        memory_mb: Allocated memory in MB (> 0).
        region: AWS region code (e.g. ``"us-east-1"``).

    Returns:
        Dict with:
        - ``cost_usd``: Total cost as a :class:`~decimal.Decimal`.
        - ``details``: Breakdown dict with ``region``, ``duration_ms``,
          ``memory_mb``, ``gb_seconds``, ``duration_cost_usd``,
          ``request_cost_usd``, ``rate_per_gb_second``.

    Raises:
        ValueError: If ``region`` is unknown, ``duration_ms`` < 0,
            or ``memory_mb`` <= 0.
    """
    # --- Input validation ---
    if duration_ms < 0:
        raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")
    if memory_mb <= 0:
        raise ValueError(f"memory_mb must be > 0, got {memory_mb}")

    data = _load_pricing()
    region_pricing = data["regions"].get(region)
    if region_pricing is None:
        supported = ", ".join(sorted(data["regions"].keys()))
        raise ValueError(
            f"Unknown AWS region '{region}'. Supported regions: {supported}"
        )

    # --- Compute GB-seconds ---
    duration_seconds = Decimal(str(duration_ms)) / Decimal("1000")
    memory_gb = Decimal(str(memory_mb)) / Decimal("1024")
    gb_seconds = duration_seconds * memory_gb

    # --- Look up rates ---
    rate_per_gb_second = Decimal(region_pricing["duration_per_gb_second"])
    request_charge = Decimal(region_pricing["request_per_invocation"])

    # --- Calculate costs ---
    duration_cost = gb_seconds * rate_per_gb_second
    total_cost = duration_cost + request_charge

    return {
        "cost_usd": total_cost,
        "details": {
            "region": region,
            "duration_ms": duration_ms,
            "memory_mb": memory_mb,
            "gb_seconds": gb_seconds,
            "duration_cost_usd": duration_cost,
            "request_cost_usd": request_charge,
            "rate_per_gb_second": rate_per_gb_second,
        },
    }
