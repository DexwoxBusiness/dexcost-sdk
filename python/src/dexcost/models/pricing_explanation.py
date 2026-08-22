"""Immutable local pricing provenance and explanation models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

from dexcost.models._serde import canonical_decimal

PricingExplanationStatus = Literal["provider_reported", "provisional", "unpriced"]


@dataclass(frozen=True)
class PricingProvenance:
    """Catalog facts active when an SDK pricing decision was recorded."""

    catalog_source: str
    stale: bool
    release_id: str | None = None
    release_sequence: int | None = None
    artifact_kind: str | None = None
    artifact_sha256: str | None = None
    artifact_schema_version: str | None = None
    observer_rules_sha256: str | None = None
    safety_policy_version: str | None = None
    workspace_overlay: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_source, str) or not self.catalog_source:
            raise ValueError("catalog_source must be a non-empty string")
        if not isinstance(self.stale, bool) or not isinstance(self.workspace_overlay, bool):
            raise TypeError("pricing provenance flags must be booleans")
        if self.release_sequence is not None and (
            isinstance(self.release_sequence, bool) or self.release_sequence < 1
        ):
            raise ValueError("release_sequence must be a positive integer")
        for name in (
            "release_id",
            "artifact_kind",
            "artifact_schema_version",
            "safety_policy_version",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("artifact_sha256", "observer_rules_sha256"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "catalog_source": self.catalog_source,
            "stale": self.stale,
            "workspace_overlay": self.workspace_overlay,
        }
        for key in (
            "release_id",
            "release_sequence",
            "artifact_kind",
            "artifact_sha256",
            "artifact_schema_version",
            "observer_rules_sha256",
            "safety_policy_version",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PricingProvenance:
        allowed = {
            "catalog_source",
            "stale",
            "workspace_overlay",
            "release_id",
            "release_sequence",
            "artifact_kind",
            "artifact_sha256",
            "artifact_schema_version",
            "observer_rules_sha256",
            "safety_policy_version",
        }
        if not isinstance(value, dict):
            raise TypeError("pricing provenance must be a dictionary")
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown pricing provenance fields: {sorted(unknown)}")
        return cls(
            catalog_source=cast(str, value.get("catalog_source")),
            stale=cast(bool, value.get("stale")),
            workspace_overlay=cast(bool, value.get("workspace_overlay", False)),
            release_id=cast(str | None, value.get("release_id")),
            release_sequence=cast(int | None, value.get("release_sequence")),
            artifact_kind=cast(str | None, value.get("artifact_kind")),
            artifact_sha256=cast(str | None, value.get("artifact_sha256")),
            artifact_schema_version=cast(
                str | None, value.get("artifact_schema_version")
            ),
            observer_rules_sha256=cast(
                str | None, value.get("observer_rules_sha256")
            ),
            safety_policy_version=cast(
                str | None, value.get("safety_policy_version")
            ),
        )


@dataclass(frozen=True)
class PricingExplanation:
    """Local, non-authoritative explanation of one durable SDK event."""

    event_id: str
    task_id: str
    event_type: str
    component: str
    status: PricingExplanationStatus
    authority: Literal["sdk_evidence"]
    amount_usd: Decimal
    confidence: str
    pricing_source: str | None
    pricing_version: str | None
    selected_rule: str | None
    inputs: tuple[tuple[str, str], ...]
    provenance: PricingProvenance | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "component": self.component,
            "status": self.status,
            "authority": self.authority,
            "amount_usd": canonical_decimal(self.amount_usd),
            "confidence": self.confidence,
            "pricing_source": self.pricing_source,
            "pricing_version": self.pricing_version,
            "selected_rule": self.selected_rule,
            "inputs": dict(self.inputs),
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
        }


__all__ = [
    "PricingExplanation",
    "PricingExplanationStatus",
    "PricingProvenance",
]
