"""Usage-only observers for services intentionally withheld from SDK pricing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_DATA_PATH = Path(__file__).parent / "data" / "service_usage_observers.json"
_METRICS = {"input_tokens", "audio_seconds"}
_COMPONENTS = {"external", "speech_to_text"}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageObserver:
    service_key: str
    provider_name: str
    provider_service: str
    component: str
    domains: tuple[str, ...]
    endpoints: tuple[str, ...]
    response_path: str
    usage_metric: str
    resource_path: str | None
    record_id_path: str | None
    record_id_header: str | None
    source_url: str


@dataclass(frozen=True)
class ServiceUsageObservation:
    service_key: str
    provider_name: str
    provider_service: str
    component: str
    metric: str
    quantity: Decimal
    manifest_version: str
    resource_id: str | None = None
    provider_record_id: str | None = None


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _bounded_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:256]


class ServiceUsageObservers:
    def __init__(self, data_path: Path | None = None) -> None:
        raw = json.loads((data_path or _DATA_PATH).read_text(encoding="utf-8"))
        meta = raw.get("_meta") if isinstance(raw, dict) else None
        definitions = raw.get("observers") if isinstance(raw, dict) else None
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("version"), str)
            or isinstance(meta.get("observer_count"), bool)
            or not isinstance(meta.get("observer_count"), int)
            or not isinstance(definitions, list)
            or meta["observer_count"] != len(definitions)
        ):
            raise ValueError("usage observer manifest metadata is inconsistent")
        self.manifest_version = meta["version"]
        self._observers: list[UsageObserver] = []
        keys: set[str] = set()
        for definition in definitions:
            if not isinstance(definition, dict):
                raise ValueError("usage observer must be an object")
            required = (
                "service_key", "provider_name", "provider_service", "component",
                "response_path", "usage_metric", "source_url",
            )
            if any(
                not isinstance(definition.get(field), str) or not definition[field]
                for field in required
            ):
                raise ValueError("usage observer contains an invalid field")
            domains = definition.get("domains")
            endpoints = definition.get("endpoints")
            if (
                definition["service_key"] in keys
                or definition["usage_metric"] not in _METRICS
                or definition["component"] not in _COMPONENTS
                or not definition["source_url"].startswith("https://")
                or not isinstance(domains, list)
                or not domains
                or not all(isinstance(item, str) and item for item in domains)
                or not isinstance(endpoints, list)
                or not endpoints
                or not all(isinstance(item, str) and item.startswith("/") for item in endpoints)
            ):
                raise ValueError("usage observer manifest contains an invalid observer")
            keys.add(definition["service_key"])
            self._observers.append(
                UsageObserver(
                    service_key=definition["service_key"],
                    provider_name=definition["provider_name"],
                    provider_service=definition["provider_service"],
                    component=definition["component"],
                    domains=tuple(domains),
                    endpoints=tuple(endpoints),
                    response_path=definition["response_path"],
                    usage_metric=definition["usage_metric"],
                    resource_path=definition.get("resource_path"),
                    record_id_path=definition.get("record_id_path"),
                    record_id_header=definition.get("record_id_header"),
                    source_url=definition["source_url"],
                )
            )

    def _lookup(self, url: str) -> UsageObserver | None:
        parsed = urlparse(url)
        return next(
            (candidate for candidate in self._observers
             if parsed.hostname in candidate.domains
             and any(
                 parsed.path == endpoint or parsed.path.startswith(f"{endpoint}/")
                 for endpoint in candidate.endpoints
             )),
            None,
        )

    def matches(self, url: str) -> bool:
        return self._lookup(url) is not None

    def observe(
        self,
        url: str,
        response_headers: dict[str, str],
        response_body: dict[str, Any] | None,
    ) -> ServiceUsageObservation | None:
        if response_body is None:
            return None
        observer = self._lookup(url)
        if observer is None:
            return None
        try:
            quantity = Decimal(str(_resolve_path(response_body, observer.response_path)))
        except (InvalidOperation, ValueError):
            return None
        if not quantity.is_finite() or quantity <= 0:
            return None
        record_id = (
            _bounded_string(_resolve_path(response_body, observer.record_id_path))
            if observer.record_id_path else None
        )
        if record_id is None and observer.record_id_header:
            record_id = _bounded_string(next(
                (value for key, value in response_headers.items()
                 if key.lower() == observer.record_id_header.lower()),
                None,
            ))
        resource_id = (
            _bounded_string(_resolve_path(response_body, observer.resource_path))
            if observer.resource_path else None
        )
        return ServiceUsageObservation(
            service_key=observer.service_key,
            provider_name=observer.provider_name,
            provider_service=observer.provider_service,
            component=observer.component,
            metric=observer.usage_metric,
            quantity=quantity,
            resource_id=resource_id,
            provider_record_id=record_id,
            manifest_version=self.manifest_version,
        )


try:
    _DEFAULT_OBSERVERS: ServiceUsageObservers | None = ServiceUsageObservers()
except (OSError, ValueError, json.JSONDecodeError) as exc:
    _LOG.warning("bundled service usage observers disabled: %s", exc)
    _DEFAULT_OBSERVERS = None


def get_service_usage_observers() -> ServiceUsageObservers | None:
    return _DEFAULT_OBSERVERS
