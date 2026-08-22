"""Cost Rates Registry for non-LLM services and user-owned infrastructure.

Implements US-011: register per-service cost rates once, load/export YAML
configs, and compute costs from ``record_usage()`` without specifying
``cost_usd`` each time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


@dataclass(frozen=True)
class InfrastructureRateEntry:
    """An explicit user-owned GPU or network rate.

    Infrastructure rates are never inferred. The normalized ``key`` must
    match the observed resource (for example ``nvidia-geforce-rtx-5060-ti``)
    or the reserved network key ``local``.
    """

    kind: str
    key: str
    per: str
    cost_usd: Decimal


_INFRASTRUCTURE_UNITS = {
    "gpu": frozenset({"gpu_second", "gpu_hour"}),
    "network": frozenset({"gb_transferred", "gb_egress"}),
}


def normalize_infrastructure_key(value: str) -> str:
    """Return the canonical exact-match key used by infrastructure rates."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _positive_decimal(value: Decimal | str, *, path: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{path} must be a positive finite decimal.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{path} must be a positive finite decimal.")
    return amount


def _decimal_text(value: Decimal) -> str:
    """Canonical plain decimal text shared with the TypeScript SDK."""
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


class RateRegistry:
    """Registry of per-service cost rates for non-LLM services.

    Stores rates and computes a ``pricing_version`` hash that changes
    whenever rates are added or modified.
    """

    def __init__(self) -> None:
        self._rates: dict[str, RateEntry] = {}
        self._infrastructure_rates: dict[tuple[str, str], InfrastructureRateEntry] = {}
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

    def register_infrastructure(
        self,
        kind: str,
        key: str,
        per: str,
        cost_usd: Decimal | str,
    ) -> None:
        """Register an explicit GPU or network rate.

        Supported units are ``gpu_second``/``gpu_hour`` for GPU and
        ``gb_transferred``/``gb_egress`` for network. Rates must be positive;
        omit a rate to keep that infrastructure usage visibly unpriced.
        """
        normalized_kind = kind.strip().lower()
        allowed_units = _INFRASTRUCTURE_UNITS.get(normalized_kind)
        if allowed_units is None:
            raise ValueError("Infrastructure kind must be 'gpu' or 'network'.")
        normalized_key = normalize_infrastructure_key(key)
        if not normalized_key:
            raise ValueError("Infrastructure rate key cannot be empty.")
        normalized_per = per.strip().lower()
        if normalized_per not in allowed_units:
            expected = ", ".join(sorted(allowed_units))
            raise ValueError(
                f"Infrastructure rate {normalized_kind}.{normalized_key}.per "
                f"must be one of: {expected}."
            )
        entry = InfrastructureRateEntry(
            kind=normalized_kind,
            key=normalized_key,
            per=normalized_per,
            cost_usd=_positive_decimal(
                cost_usd,
                path=f"Infrastructure rate {normalized_kind}.{normalized_key}.cost_usd",
            ),
        )
        self._infrastructure_rates[(normalized_kind, normalized_key)] = entry
        self._version = None

    def get_infrastructure(
        self,
        kind: str,
        key: str,
    ) -> InfrastructureRateEntry | None:
        """Return an exact normalized infrastructure rate, or ``None``."""
        return self._infrastructure_rates.get(
            (kind.strip().lower(), normalize_infrastructure_key(key))
        )

    @property
    def rates(self) -> dict[str, RateEntry]:
        """A copy of all registered rates."""
        return dict(self._rates)

    @property
    def infrastructure_rates(self) -> dict[tuple[str, str], InfrastructureRateEntry]:
        """A copy of all registered infrastructure rates."""
        return dict(self._infrastructure_rates)

    @property
    def pricing_version(self) -> str:
        """A deterministic hash of all registered rates, for reproducibility."""
        if self._version is None:
            self._version = self._compute_version()
        return self._version

    def load(self, path: str | Path) -> None:
        """Load rates from a YAML config file.

        Expected format::

            version: 2
            rates:
              maps.googleapis.com:
                per: request
                cost_usd: "0.005"
              ocr-api.com:
                per: page
                cost_usd: "0.01"
            infrastructure:
              gpu:
                nvidia-geforce-rtx-5060-ti:
                  per: gpu_hour
                  cost_usd: "0.25"
              network:
                local:
                  per: gb_transferred
                  cost_usd: "0.02"

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
            parsed: Any = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in rates file {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Expected a mapping at the root of the YAML file.")
        version = parsed.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
            raise ValueError("Rates YAML 'version' must be 1 or 2.")
        rates_data = parsed.get("rates", {})
        if not isinstance(rates_data, dict):
            raise ValueError("Expected 'rates' key with a mapping in the YAML file.")
        pending_rates: list[tuple[str, str, str]] = []
        for service, info in rates_data.items():
            if not isinstance(info, dict) or "cost_usd" not in info:
                raise ValueError(
                    f"Rate entry for {service!r} must be a mapping with at least 'cost_usd'."
                )
            pending_rates.append(
                (str(service), str(info.get("per", "unit")), str(info["cost_usd"]))
            )
        infrastructure_data = parsed.get("infrastructure", {})
        if not isinstance(infrastructure_data, dict):
            raise ValueError("Expected 'infrastructure' to be a mapping in the YAML file.")
        if infrastructure_data and version != 2:
            raise ValueError("Infrastructure rates require rates YAML version: 2.")
        unknown_kinds = set(infrastructure_data) - set(_INFRASTRUCTURE_UNITS)
        if unknown_kinds:
            names = ", ".join(sorted(str(kind) for kind in unknown_kinds))
            raise ValueError(f"Unsupported infrastructure rate kind(s): {names}.")
        pending_infrastructure: list[tuple[str, str, str, str]] = []
        for kind, entries in infrastructure_data.items():
            if not isinstance(entries, dict):
                raise ValueError(f"Expected 'infrastructure.{kind}' to be a mapping.")
            normalized_keys: set[str] = set()
            for key, info in entries.items():
                if not isinstance(key, str):
                    raise ValueError(f"Infrastructure rate key in {kind!r} must be a string.")
                normalized_key = normalize_infrastructure_key(key)
                if normalized_key in normalized_keys:
                    raise ValueError(
                        f"Infrastructure rate {kind}.{key} duplicates normalized key "
                        f"{normalized_key!r}."
                    )
                normalized_keys.add(normalized_key)
                if not isinstance(info, dict) or "per" not in info or "cost_usd" not in info:
                    raise ValueError(
                        f"Infrastructure rate {kind}.{key} must contain 'per' and 'cost_usd'."
                    )
                pending_infrastructure.append(
                    (str(kind), key, str(info["per"]), str(info["cost_usd"]))
                )
        # Validate infrastructure values before mutating this registry so a
        # bad file cannot leave a partially loaded pricing snapshot.
        validator = RateRegistry()
        for service, per, cost_usd in pending_rates:
            validator.register(service, per, cost_usd)
        for kind, key, per, cost_usd in pending_infrastructure:
            validator.register_infrastructure(kind, key, per, cost_usd)
        for service, per, cost_usd in pending_rates:
            self.register(service, per, cost_usd)
        for kind, key, per, cost_usd in pending_infrastructure:
            self.register_infrastructure(kind, key, per, cost_usd)

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
            service_entry = self._rates[service]
            rates_data[service] = {
                "per": service_entry.per,
                "cost_usd": _decimal_text(service_entry.cost_usd),
            }
        infrastructure_data: dict[str, dict[str, dict[str, str]]] = {}
        for kind in sorted(_INFRASTRUCTURE_UNITS):
            entries: dict[str, dict[str, str]] = {}
            for entry_kind, key in sorted(self._infrastructure_rates):
                if entry_kind != kind:
                    continue
                infrastructure_entry = self._infrastructure_rates[(entry_kind, key)]
                entries[key] = {
                    "per": infrastructure_entry.per,
                    "cost_usd": _decimal_text(infrastructure_entry.cost_usd),
                }
            if entries:
                infrastructure_data[kind] = entries
        payload: dict[str, Any] = {"version": 2, "rates": rates_data}
        if infrastructure_data:
            payload["infrastructure"] = infrastructure_data
        output: str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
        try:
            Path(path).write_text(output, encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Cannot write rates file {path}: {exc}") from exc

    def _compute_version(self) -> str:
        """Compute SHA-256 hash prefix of all rates for ``pricing_version``."""
        parts: list[str] = []
        for service in sorted(self._rates):
            service_entry = self._rates[service]
            # Preserve the established service-only pricing version.
            parts.append(
                f"{service}:{service_entry.per}:{_decimal_text(service_entry.cost_usd)}"
            )
        for kind, key in sorted(self._infrastructure_rates):
            infrastructure_entry = self._infrastructure_rates[(kind, key)]
            parts.append(
                "infrastructure:"
                f"{kind}:{key}:{infrastructure_entry.per}:"
                f"{_decimal_text(infrastructure_entry.cost_usd)}"
            )
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
