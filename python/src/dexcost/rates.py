"""Cost Rates Registry for non-LLM services.

Implements US-011: register per-service cost rates once, load/export YAML
configs, and compute costs from ``record_usage()`` without specifying
``cost_usd`` each time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RateEntry:
    """A registered per-unit cost rate for a non-LLM service.

    Attributes:
        service: Service identifier (e.g. ``"maps.googleapis.com"``).
        per: Unit label (e.g. ``"request"``, ``"page"``).
        cost_usd: Cost per unit in USD.
    """

    service: str
    per: str
    cost_usd: Decimal


class RateRegistry:
    """Registry of per-service cost rates for non-LLM services.

    Stores rates and computes a ``pricing_version`` hash that changes
    whenever rates are added or modified.
    """

    def __init__(self) -> None:
        self._rates: dict[str, RateEntry] = {}
        self._version: str | None = None

    def register(self, service: str, per: str, cost_usd: Decimal | str) -> None:
        """Register a per-unit cost rate for *service*.

        Args:
            service: Service identifier (e.g. ``"maps.googleapis.com"``).
            per: What a "unit" means (e.g. ``"request"``, ``"page"``).
            cost_usd: Cost per unit in USD.
        """
        entry = RateEntry(service=service, per=per, cost_usd=Decimal(str(cost_usd)))
        self._rates[service] = entry
        self._version = None  # Invalidate cached version

    def get(self, service: str) -> RateEntry | None:
        """Return the rate entry for *service*, or ``None``."""
        return self._rates.get(service)

    @property
    def rates(self) -> dict[str, RateEntry]:
        """A copy of all registered rates."""
        return dict(self._rates)

    @property
    def pricing_version(self) -> str:
        """A deterministic hash of all registered rates, for reproducibility."""
        if self._version is None:
            self._version = self._compute_version()
        return self._version

    def load(self, path: str | Path) -> None:
        """Load rates from a YAML config file.

        Expected format::

            rates:
              maps.googleapis.com:
                per: request
                cost_usd: "0.005"
              ocr-api.com:
                per: page
                cost_usd: "0.01"

        Args:
            path: Path to the YAML file.

        Raises:
            ValueError: If the YAML structure is invalid.
        """
        import yaml

        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"Cannot read rates file {path}: {exc}") from exc
        try:
            parsed: dict[str, Any] = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in rates file {path}: {exc}") from exc
        rates_data = parsed.get("rates", {})
        if not isinstance(rates_data, dict):
            raise ValueError("Expected 'rates' key with a mapping in the YAML file.")
        for service, info in rates_data.items():
            if not isinstance(info, dict) or "cost_usd" not in info:
                raise ValueError(
                    f"Rate entry for {service!r} must be a mapping with at least 'cost_usd'."
                )
            self.register(
                service=str(service),
                per=str(info.get("per", "unit")),
                cost_usd=str(info["cost_usd"]),
            )

    def export(self, path: str | Path) -> None:
        """Export current rates to a YAML config file.

        The output is deterministically sorted by service name so that
        the file is suitable for version control (``rates.yaml`` committed
        to the user's repo).

        Args:
            path: Path to write the YAML file.
        """
        import yaml

        rates_data: dict[str, dict[str, str]] = {}
        for service in sorted(self._rates):
            entry = self._rates[service]
            rates_data[service] = {
                "per": entry.per,
                "cost_usd": str(entry.cost_usd),
            }
        output: str = yaml.dump({"rates": rates_data}, default_flow_style=False, sort_keys=False)
        try:
            Path(path).write_text(output, encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Cannot write rates file {path}: {exc}") from exc

    def _compute_version(self) -> str:
        """Compute SHA-256 hash prefix of all rates for ``pricing_version``."""
        parts: list[str] = []
        for service in sorted(self._rates):
            entry = self._rates[service]
            parts.append(f"{service}:{entry.per}:{entry.cost_usd}")
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
