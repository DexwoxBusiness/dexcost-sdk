"""Egress pricing engine — resolves a per-GB egress rate from
``(provider, region)`` using the bundled ``data/egress_prices.json`` catalog.

Mirrors :mod:`dexcost.pricing` in shape: bundled JSON + a resolver returning
``(rate, pricing_source, cost_confidence)``.

Fail-silent contract: every failure mode degrades through the spec §7.1
ladder; the engine always returns a usable :class:`EgressRate`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Tier-4 ultimate fallback — used only when the catalog cannot be read at all
# AND _meta.default_rate_usd_per_gb cannot be resolved.  Matches the spec §7.1
# hardcoded constant.
_HARDCODED_DEFAULT = Decimal("0.09")

_warned_modes: set[str] = set()
_warn_lock = threading.Lock()


def _reset_warning_state() -> None:
    """Test-only: clear the warn-once tracking set."""
    with _warn_lock:
        _warned_modes.clear()


def _warn_once(mode: str, message: str) -> None:
    with _warn_lock:
        if mode in _warned_modes:
            return
        _warned_modes.add(mode)
    _log.warning(message)


@dataclass(frozen=True)
class EgressRate:
    """The result of an egress-rate lookup."""

    rate_per_gb: Decimal
    pricing_source: str
    cost_confidence: str  # exact | computed | estimated


class EgressPricingEngine:
    """Resolve egress rates from the bundled catalog.

    Args:
        catalog_path: Optional override path. ``None`` uses the bundled
            ``data/egress_prices.json``.
    """

    def __init__(self, catalog_path: str | Path | None = None) -> None:
        self._catalog: dict[str, Any] = {}
        self._catalog_path = catalog_path
        self._catalog_version: str = "unknown"
        self._load()

    def _load(self) -> None:
        try:
            if self._catalog_path is not None:
                raw = Path(self._catalog_path).read_text(encoding="utf-8")
            else:
                raw = (
                    resources.files("dexcost")
                    .joinpath("data")
                    .joinpath("egress_prices.json")
                    .read_text(encoding="utf-8")
                )
        except FileNotFoundError:
            _warn_once(
                "catalog_missing",
                "egress catalog file not found; falling back to hardcoded default",
            )
            return
        except OSError as exc:
            _warn_once(
                "catalog_unreadable",
                f"egress catalog unreadable ({exc}); falling back to hardcoded default",
            )
            return

        try:
            self._catalog = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn_once(
                "catalog_malformed",
                f"egress catalog malformed JSON ({exc}); falling back to hardcoded default",
            )
            self._catalog = {}
            return

        meta = self._catalog.get("_meta", {})
        self._catalog_version = str(meta.get("version", "unknown"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    def rate_for_internal(self) -> EgressRate:
        """Rate for a call classified as internal traffic — always free."""
        return EgressRate(
            rate_per_gb=Decimal("0"),
            pricing_source="egress_catalog:internal",
            cost_confidence="exact",
        )

    def resolve_rate(self, provider: str | None, region: str | None) -> EgressRate:
        """Resolve an egress rate via the §7.1 degradation ladder.

        Tier 1: ``(provider, region)`` exact match → region rate, ``computed``.
        Tier 2: provider known, region absent/unknown → provider default,
            ``estimated``.
        Tier 3: provider not detected / not in catalog → ``_meta`` default,
            ``estimated``.
        Tier 4: catalog unreadable or ``_meta`` default absent → hardcoded
            ``Decimal("0.09")``, ``estimated``.
        """
        if provider:
            block = self._catalog.get(provider)
            if isinstance(block, dict):
                regions = block.get("regions", {})
                if region and region in regions:
                    try:
                        rate = Decimal(str(regions[region]))
                    except InvalidOperation:
                        _warn_once(
                            f"region_rate_malformed:{provider}:{region}",
                            f"egress region rate malformed for {provider}/{region}",
                        )
                    else:
                        return EgressRate(
                            rate_per_gb=rate,
                            pricing_source=f"egress_catalog:{provider}:{region}",
                            cost_confidence="computed",
                        )
                try:
                    prov_default = Decimal(str(block.get("default_usd_per_gb", "")))
                except (InvalidOperation, TypeError):
                    prov_default = None  # type: ignore[assignment]
                if prov_default is not None:
                    return EgressRate(
                        rate_per_gb=prov_default,
                        pricing_source=f"egress_catalog:{provider}:default",
                        cost_confidence="estimated",
                    )

        meta = self._catalog.get("_meta") if self._catalog else None
        if isinstance(meta, dict):
            try:
                rate = Decimal(str(meta.get("default_rate_usd_per_gb", "")))
            except (InvalidOperation, TypeError):
                rate = None  # type: ignore[assignment]
            if rate is not None:
                return EgressRate(
                    rate_per_gb=rate,
                    pricing_source="egress_catalog:default",
                    cost_confidence="estimated",
                )
            _warn_once(
                "meta_default_missing",
                "egress _meta.default_rate_usd_per_gb missing/malformed; "
                "using hardcoded default",
            )

        return EgressRate(
            rate_per_gb=_HARDCODED_DEFAULT,
            pricing_source="egress_catalog:default",
            cost_confidence="estimated",
        )
