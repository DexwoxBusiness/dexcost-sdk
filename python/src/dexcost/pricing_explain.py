"""Privacy-safe local pricing explanations with catalog-release provenance."""

from __future__ import annotations

import re
import threading
import uuid
from collections import OrderedDict
from decimal import Decimal

from dexcost.models._serde import canonical_decimal
from dexcost.models.event import Event
from dexcost.models.pricing_explanation import (
    PricingExplanation,
    PricingExplanationStatus,
    PricingProvenance,
)
from dexcost.storage.protocol import StorageBackend

_DETAIL_KEY = "pricing_provenance"
_REGISTRY_LIMIT = 128
_REGISTRY: OrderedDict[str, PricingProvenance] = OrderedDict()
_LOCK = threading.Lock()
_RELEASE_VERSION = re.compile(
    r"^(?:(compute|gpu|egress):)?catalog-release:([1-9][0-9]*):([0-9a-f]{12})(:stale)?"
)


def register_pricing_provenance(version: str, provenance: PricingProvenance) -> None:
    """Register one exact in-process pricing version for future event snapshots."""
    if not isinstance(version, str) or not version:
        raise ValueError("pricing version must be a non-empty string")
    with _LOCK:
        _REGISTRY[version] = provenance
        _REGISTRY.move_to_end(version)
        while len(_REGISTRY) > _REGISTRY_LIMIT:
            _REGISTRY.popitem(last=False)


def _inferred_provenance(event: Event) -> PricingProvenance | None:
    version = event.pricing_version
    if version is None:
        return None
    match = _RELEASE_VERSION.match(version)
    if match is None:
        return PricingProvenance(
            catalog_source="local_or_bootstrap",
            stale=False,
            artifact_kind=(
                "llm_prices"
                if event.event_type == "llm_call"
                else "service_prices"
                if event.pricing_source == "service_catalog"
                else None
            ),
            workspace_overlay=(event.pricing_source or "").startswith("workspace_overlay"),
        )
    prefix, sequence, _short_hash, stale = match.groups()
    return PricingProvenance(
        catalog_source="catalog_release",
        stale=stale is not None,
        release_sequence=int(sequence),
        artifact_kind={
            None: "llm_prices",
            "compute": "compute_prices",
            "gpu": "gpu_prices",
            "egress": "egress_prices",
        }[prefix],
        workspace_overlay=(event.pricing_source or "").startswith("workspace_overlay"),
    )


def pricing_provenance_for_event(event: Event) -> PricingProvenance | None:
    durable = event.details.get(_DETAIL_KEY)
    if durable is not None:
        if not isinstance(durable, dict):
            raise ValueError("pricing_provenance must be a dictionary")
        return PricingProvenance.from_dict(durable)
    version = event.pricing_version
    if version is not None:
        with _LOCK:
            registered = _REGISTRY.get(version)
        if registered is not None:
            return registered
    return _inferred_provenance(event)


def apply_event_pricing_provenance(event: Event) -> Event:
    """Persist the exact catalog snapshot active for this event, when known."""
    provenance = pricing_provenance_for_event(event)
    if provenance is not None:
        event.details = {**event.details, _DETAIL_KEY: provenance.to_dict()}
    return event


def _component(event: Event) -> str:
    explicit = event.details.get("attribution_component")
    if isinstance(explicit, str) and explicit:
        return explicit
    return {
        "llm_call": "llm",
        "compute_cost": "compute",
        "gpu_cost": "gpu",
        "network": "network",
    }.get(event.event_type, "external")


def _inputs(event: Event) -> tuple[tuple[str, str], ...]:
    values: dict[str, str] = {}
    if event.model is not None:
        values["model"] = event.model
    if event.service_name is not None:
        values["service"] = event.service_name
    for key in ("input_tokens", "output_tokens", "cached_tokens", "latency_ms"):
        value = getattr(event, key)
        if value is not None:
            values[key] = str(value)
    for key in (
        "attribution_usage_metric",
        "attribution_usage_quantity",
        "attribution_usage_unit",
        "attribution_usage_duration_seconds",
        "cache_creation_input_tokens",
        "billing_model",
        "cloud_provider",
        "region",
        "gpu_sku",
        "runtime",
    ):
        value = event.details.get(key)
        if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
            values[key] = canonical_decimal(value) if isinstance(value, Decimal) else str(value)
    return tuple(sorted(values.items()))


def explain_event_pricing(event: Event) -> PricingExplanation:
    """Explain one event locally without consulting the network or re-pricing it."""
    source = event.pricing_source
    status: PricingExplanationStatus
    if source in {"provider_response", "provider_reported"}:
        status = "provider_reported"
    elif event.cost_confidence == "unknown" or source is None or (
        source == "unknown" or source.startswith("unpriced:")
    ):
        status = "unpriced"
    else:
        status = "provisional"
    return PricingExplanation(
        event_id=str(event.event_id),
        task_id=str(event.task_id),
        event_type=event.event_type,
        component=_component(event),
        status=status,
        authority="sdk_evidence",
        amount_usd=event.cost_usd,
        confidence=event.cost_confidence,
        pricing_source=source,
        pricing_version=event.pricing_version,
        selected_rule=source,
        inputs=_inputs(event),
        provenance=pricing_provenance_for_event(event),
    )


def explain_stored_pricing(
    storage: StorageBackend,
    event_or_id: Event | uuid.UUID | str,
) -> PricingExplanation:
    """Resolve a durable event when needed, then explain its recorded decision."""
    if isinstance(event_or_id, Event):
        return explain_event_pricing(event_or_id)
    try:
        event_id = event_or_id if isinstance(event_or_id, uuid.UUID) else uuid.UUID(event_or_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("event_id must be a valid UUID") from exc
    events = storage.query_events(event_id=str(event_id))
    if not events:
        raise KeyError(f"event {event_id} was not found in local storage")
    return explain_event_pricing(events[0])


__all__ = [
    "apply_event_pricing_provenance",
    "explain_event_pricing",
    "explain_stored_pricing",
    "pricing_provenance_for_event",
    "register_pricing_provenance",
]
