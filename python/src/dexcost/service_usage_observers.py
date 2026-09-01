"""Usage-only observers for services intentionally withheld from SDK pricing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

_DATA_PATH = Path(__file__).parent / "data" / "service_usage_observers.json"
_METRICS = {
    "input_tokens",
    "audio_seconds",
    "characters",
    "request_count",
    "credit_count",
}
_COMPONENTS = {"external", "speech_to_text", "text_to_speech"}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageObserver:
    service_key: str
    provider_name: str
    provider_service: str
    component: str
    domains: tuple[str, ...]
    endpoints: tuple[str, ...]
    endpoint_match: str
    response_path: str | None
    response_quantity_header: str | None
    response_all: tuple[dict[str, Any], ...]
    request_all: tuple[dict[str, Any], ...]
    request_character_count_path: str | None
    minimum_quantity: str | None
    fixed_quantity: str | None
    usage_metric: str
    resource_type: str | None
    resource_path: str | None
    request_resource_path: str | None
    allowed_resource_ids: tuple[str, ...]
    resource_query_parameter: str | None
    default_resource_id: str | None
    fixed_resource_id: str | None
    resource_variant: dict[str, str] | None
    query_any: tuple[dict[str, str], ...]
    quantity_multiplier_path: str | None
    quantity_multiplier_query_parameter: str | None
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
    resource_type: str | None = None
    resource_id: str | None = None
    provider_record_id: str | None = None
    dimensions: tuple[dict[str, Any], ...] = ()


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


def _character_count(value: Any) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sum(len(item) for item in value)
    return None


def _query_value_is_truthy(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _response_predicate_matches(value: Any, predicate: dict[str, Any]) -> bool:
    resolved = _resolve_path(value, predicate["path"])
    if predicate["operator"] == "equals":
        return resolved == predicate["value"] and type(resolved) is type(predicate["value"])
    if isinstance(resolved, str):
        return bool(resolved.strip())
    return isinstance(resolved, (list, dict)) and bool(resolved)


def _valid_response_predicate(predicate: Any) -> bool:
    if not isinstance(predicate, dict) or not isinstance(predicate.get("path"), str):
        return False
    if not predicate["path"]:
        return False
    if predicate.get("operator") == "non_empty":
        return set(predicate) == {"path", "operator"}
    value = predicate.get("value")
    return (
        predicate.get("operator") == "equals"
        and set(predicate) == {"path", "operator", "value"}
        and type(value) in {str, bool}
    )


def _request_predicate_matches(value: Any, predicate: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    resolved = _resolve_path(value, predicate["path"])
    if resolved is None:
        return True
    if predicate["operator"] == "absent_or_false_or_null":
        return resolved is False
    return (
        predicate["operator"] == "absent_or_lte"
        and isinstance(resolved, (int, float))
        and not isinstance(resolved, bool)
        and resolved <= predicate["value"]
    )


def _valid_request_predicate(predicate: Any) -> bool:
    if not isinstance(predicate, dict) or not isinstance(predicate.get("path"), str):
        return False
    if not predicate["path"]:
        return False
    if predicate.get("operator") in {"absent_or_null", "absent_or_false_or_null"}:
        return set(predicate) == {"path", "operator"}
    value = predicate.get("value")
    return (
        predicate.get("operator") == "absent_or_lte"
        and set(predicate) == {"path", "operator", "value"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


class ServiceUsageObservers:
    def __init__(
        self,
        data_path: Path | None = None,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        if data_path is not None and data is not None:
            raise ValueError("data_path and data are mutually exclusive")
        raw = data if data is not None else json.loads(
            (data_path or _DATA_PATH).read_text(encoding="utf-8")
        )
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
                "service_key",
                "provider_name",
                "provider_service",
                "component",
                "usage_metric",
                "source_url",
            )
            if any(
                not isinstance(definition.get(field), str) or not definition[field]
                for field in required
            ):
                raise ValueError("usage observer contains an invalid field")
            domains = definition.get("domains")
            endpoints = definition.get("endpoints")
            optional_string_fields = (
                "resource_path",
                "request_resource_path",
                "request_character_count_path",
                "minimum_quantity",
                "response_quantity_header",
                "fixed_quantity",
                "resource_query_parameter",
                "default_resource_id",
                "fixed_resource_id",
                "quantity_multiplier_path",
                "quantity_multiplier_query_parameter",
                "record_id_path",
                "record_id_header",
                "endpoint_match",
            )
            has_resource_selector = any(
                field in definition
                for field in (
                    "resource_path",
                    "request_resource_path",
                    "resource_query_parameter",
                    "default_resource_id",
                    "fixed_resource_id",
                )
            )
            response_path = definition.get("response_path")
            response_quantity_header = definition.get("response_quantity_header")
            response_all = definition.get("response_all", [])
            request_all = definition.get("request_all", [])
            request_character_count_path = definition.get("request_character_count_path")
            minimum_quantity = definition.get("minimum_quantity")
            fixed_quantity = definition.get("fixed_quantity")
            allowed_resource_ids = definition.get("allowed_resource_ids", [])
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
                or definition.get("endpoint_match", "prefix") not in {"exact", "prefix"}
                or any(
                    field in definition
                    and (not isinstance(definition[field], str) or not definition[field])
                    for field in optional_string_fields
                )
                or definition.get("resource_type") not in {None, "model", "sku"}
                or sum(
                    value is not None
                    for value in (
                        response_path,
                        response_quantity_header,
                        request_character_count_path,
                        fixed_quantity,
                    )
                )
                != 1
                or fixed_quantity not in {None, "1"}
                or minimum_quantity not in {None, "1"}
                or (minimum_quantity is not None and request_character_count_path is None)
                or (fixed_quantity is not None)
                != (definition["usage_metric"] == "request_count")
                or (
                    response_path is not None
                    and (not isinstance(response_path, str) or not response_path)
                )
                or not isinstance(allowed_resource_ids, list)
                or any(not isinstance(item, str) or not item for item in allowed_resource_ids)
                or (allowed_resource_ids and definition.get("resource_type") is None)
                or (has_resource_selector and definition.get("resource_type") is None)
                or (
                    "quantity_multiplier_query_parameter" in definition
                    and "quantity_multiplier_path" not in definition
                )
            ):
                raise ValueError("usage observer manifest contains an invalid observer")
            if (
                not isinstance(response_all, list)
                or ("response_all" in definition and not response_all)
                or not all(_valid_response_predicate(item) for item in response_all)
            ):
                raise ValueError("usage observer manifest contains an invalid response predicate")
            if (
                not isinstance(request_all, list)
                or ("request_all" in definition and not request_all)
                or not all(_valid_request_predicate(item) for item in request_all)
            ):
                raise ValueError("usage observer manifest contains an invalid request predicate")
            query_any = definition.get("query_any", [])
            if (
                not isinstance(query_any, list)
                or ("query_any" in definition and not query_any)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("parameter"), str)
                    or item.get("operator") not in {"present", "truthy"}
                    for item in query_any
                )
            ):
                raise ValueError("usage observer manifest contains an invalid query predicate")
            resource_variant = definition.get("resource_variant")
            if resource_variant is not None and (
                not isinstance(resource_variant, dict)
                or any(
                    not isinstance(resource_variant.get(field), str) or not resource_variant[field]
                    for field in ("query_parameter", "equals", "matched_suffix", "default_suffix")
                )
            ):
                raise ValueError("usage observer manifest contains an invalid resource variant")
            keys.add(definition["service_key"])
            self._observers.append(
                UsageObserver(
                    service_key=definition["service_key"],
                    provider_name=definition["provider_name"],
                    provider_service=definition["provider_service"],
                    component=definition["component"],
                    domains=tuple(domains),
                    endpoints=tuple(endpoints),
                    endpoint_match=definition.get("endpoint_match", "prefix"),
                    response_path=response_path,
                    response_quantity_header=response_quantity_header,
                    response_all=tuple(response_all),
                    request_all=tuple(request_all),
                    request_character_count_path=request_character_count_path,
                    minimum_quantity=minimum_quantity,
                    fixed_quantity=fixed_quantity,
                    usage_metric=definition["usage_metric"],
                    resource_type=definition.get("resource_type"),
                    resource_path=definition.get("resource_path"),
                    request_resource_path=definition.get("request_resource_path"),
                    allowed_resource_ids=tuple(allowed_resource_ids),
                    resource_query_parameter=definition.get("resource_query_parameter"),
                    default_resource_id=definition.get("default_resource_id"),
                    fixed_resource_id=definition.get("fixed_resource_id"),
                    resource_variant=resource_variant,
                    query_any=tuple(query_any),
                    quantity_multiplier_path=definition.get("quantity_multiplier_path"),
                    quantity_multiplier_query_parameter=definition.get(
                        "quantity_multiplier_query_parameter"
                    ),
                    record_id_path=definition.get("record_id_path"),
                    record_id_header=definition.get("record_id_header"),
                    source_url=definition["source_url"],
                )
            )

    @property
    def observer_count(self) -> int:
        return len(self._observers)

    def _lookup(self, url: str) -> tuple[Any, list[UsageObserver]] | None:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        matched = [
            candidate
            for candidate in self._observers
            if parsed.hostname in candidate.domains
            and any(
                parsed.path == endpoint
                or (
                    candidate.endpoint_match == "prefix"
                    and parsed.path.startswith(f"{endpoint}/")
                )
                for endpoint in candidate.endpoints
            )
            and (
                not candidate.query_any
                or any(
                    predicate["parameter"] in query
                    if predicate["operator"] == "present"
                    else any(
                        _query_value_is_truthy(value)
                        for value in query.get(predicate["parameter"], [])
                    )
                    for predicate in candidate.query_any
                )
            )
        ]
        return (parsed, matched) if matched else None

    def matches(self, url: str) -> bool:
        return self._lookup(url) is not None

    def owns_endpoint_boundary(self, url: str) -> bool:
        """Return whether an observer owns this provider endpoint boundary.

        Exact observer routes deliberately reject descendants and unsupported
        query variants, but those requests must not fall back to a broader
        bundled money catalog. Treat the declared endpoint and its descendants
        as observer-owned while leaving unrelated paths on the same domain
        available to other instrumentation.
        """
        parsed = urlparse(url)
        return any(
            parsed.hostname in candidate.domains
            and any(
                parsed.path == endpoint or parsed.path.startswith(f"{endpoint}/")
                for endpoint in candidate.endpoints
            )
            for candidate in self._observers
        )

    def needs_request_body(self, url: str) -> bool:
        matched = self._lookup(url)
        return bool(
            matched
            and any(
                item.request_resource_path
                or item.request_character_count_path
                or item.request_all
                for item in matched[1]
            )
        )

    def observe(
        self,
        url: str,
        response_headers: dict[str, str],
        response_body: dict[str, Any] | None,
        request_body: dict[str, Any] | None = None,
    ) -> list[ServiceUsageObservation]:
        matched = self._lookup(url)
        if matched is None:
            return []
        parsed, observers = matched
        query = parse_qs(parsed.query, keep_blank_values=True)
        observations: list[ServiceUsageObservation] = []
        for observer in observers:
            if not all(
                _request_predicate_matches(request_body, predicate)
                for predicate in observer.request_all
            ):
                continue
            if not all(
                _response_predicate_matches(response_body, predicate)
                for predicate in observer.response_all
            ):
                continue
            if observer.request_character_count_path:
                character_count = _character_count(
                    _resolve_path(request_body, observer.request_character_count_path)
                )
                if character_count is None:
                    continue
                if observer.minimum_quantity == "1":
                    character_count = max(character_count, 1)
                quantity = Decimal(character_count)
            elif observer.fixed_quantity:
                quantity = Decimal(observer.fixed_quantity)
            elif observer.response_quantity_header:
                raw_quantity = next(
                    (
                        value
                        for key, value in response_headers.items()
                        if key.lower() == observer.response_quantity_header.lower()
                    ),
                    None,
                )
                try:
                    quantity = Decimal(str(raw_quantity))
                except (InvalidOperation, ValueError):
                    continue
            else:
                try:
                    quantity = Decimal(
                        str(_resolve_path(response_body, observer.response_path or ""))
                    )
                except (InvalidOperation, ValueError):
                    continue
            if not quantity.is_finite() or quantity <= 0:
                continue
            if observer.quantity_multiplier_path and (
                observer.quantity_multiplier_query_parameter is None
                or any(
                    _query_value_is_truthy(value)
                    for value in query.get(observer.quantity_multiplier_query_parameter, [])
                )
            ):
                try:
                    multiplier = Decimal(
                        str(_resolve_path(response_body, observer.quantity_multiplier_path))
                    )
                except (InvalidOperation, ValueError):
                    multiplier = Decimal(0)
                if multiplier.is_finite() and multiplier > 0:
                    quantity *= multiplier
            record_id = (
                _bounded_string(_resolve_path(response_body, observer.record_id_path))
                if observer.record_id_path
                else None
            )
            if record_id is None and observer.record_id_header:
                record_id = _bounded_string(
                    next(
                        (
                            value
                            for key, value in response_headers.items()
                            if key.lower() == observer.record_id_header.lower()
                        ),
                        None,
                    )
                )
            if record_id is None and observer.service_key == "elevenlabs_tts":
                record_id = _bounded_string(
                    next(
                        (
                            value
                            for key, value in response_headers.items()
                            if key.lower() == "x-trace-id"
                        ),
                        None,
                    )
                )
            resource_id = (
                _bounded_string(_resolve_path(response_body, observer.resource_path))
                if observer.resource_path
                else None
            )
            request_resource_id = (
                _bounded_string(_resolve_path(request_body, observer.request_resource_path))
                if observer.request_resource_path
                else None
            )
            query_resource_id = (
                _bounded_string(next(iter(query.get(observer.resource_query_parameter, [])), None))
                if observer.resource_query_parameter
                else None
            )
            if (
                request_resource_id is not None
                and query_resource_id is not None
                and request_resource_id != query_resource_id
            ):
                continue
            resource_id = resource_id or request_resource_id or query_resource_id
            resource_id = resource_id or _bounded_string(observer.fixed_resource_id)
            resource_id = resource_id or _bounded_string(observer.default_resource_id)
            if observer.allowed_resource_ids and resource_id not in observer.allowed_resource_ids:
                continue
            if resource_id is not None and observer.resource_variant is not None:
                variant = observer.resource_variant
                suffix = (
                    variant["matched_suffix"]
                    if next(iter(query.get(variant["query_parameter"], [])), None)
                    == variant["equals"]
                    else variant["default_suffix"]
                )
                resource_id = f"{resource_id}{suffix}"[:256]
            dimensions: list[dict[str, Any]] = []
            if observer.service_key == "elevenlabs_tts":
                prefix = "/v1/text-to-speech/"
                if parsed.path.startswith(prefix):
                    encoded = parsed.path[len(prefix) :].split("/", 1)[0]
                    try:
                        value = _bounded_string(unquote(encoded, errors="strict"))
                    except UnicodeDecodeError:
                        value = None
                    if value is not None:
                        dimensions.append(
                            {
                                "key": "voice_id",
                                "value": {"type": "string", "value": value},
                            }
                        )
            observations.append(
                ServiceUsageObservation(
                    service_key=observer.service_key,
                    provider_name=observer.provider_name,
                    provider_service=observer.provider_service,
                    component=observer.component,
                    metric=observer.usage_metric,
                    quantity=quantity,
                    resource_type=observer.resource_type if resource_id else None,
                    resource_id=resource_id,
                    provider_record_id=record_id,
                    dimensions=tuple(dimensions),
                    manifest_version=self.manifest_version,
                )
            )
        return observations


try:
    _DEFAULT_OBSERVERS: ServiceUsageObservers | None = ServiceUsageObservers()
except (OSError, ValueError, json.JSONDecodeError) as exc:
    _LOG.warning("bundled service usage observers disabled: %s", exc)
    _DEFAULT_OBSERVERS = None


def get_service_usage_observers() -> ServiceUsageObservers | None:
    return _DEFAULT_OBSERVERS


def set_service_usage_observers(observers: ServiceUsageObservers | None) -> None:
    """Atomically replace the process-wide declarative observer set."""
    global _DEFAULT_OBSERVERS
    _DEFAULT_OBSERVERS = observers
