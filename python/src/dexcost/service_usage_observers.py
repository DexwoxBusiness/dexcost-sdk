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
    "output_tokens",
    "audio_seconds",
    "characters",
    "request_count",
    "credit_count",
}
_COMPONENTS = {"external", "speech_to_text", "text_to_speech"}
_DOMAIN_EDGE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_DOMAIN_LABEL_CHARS = _DOMAIN_EDGE_CHARS | {"-"}
_HEADER_NAME_CHARS = _DOMAIN_EDGE_CHARS | frozenset("!#$%&'*+.^_`|~-")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageObserver:
    service_key: str
    provider_name: str
    provider_service: str
    component: str
    domains: tuple[str, ...]
    domain_suffixes: tuple[str, ...]
    endpoints: tuple[str, ...]
    endpoint_match: str
    response_path: str | None
    response_quantity_header: str | None
    response_all: tuple[dict[str, Any], ...]
    request_all: tuple[dict[str, Any], ...]
    request_header_all: tuple[dict[str, str], ...]
    request_character_count_path: str | None
    request_character_count_query_parameter: str | None
    request_character_count_case_insensitive: bool
    character_count_encoding: str
    minimum_quantity: str | None
    fixed_quantity: str | None
    usage_metric: str
    resource_type: str | None
    resource_path: str | None
    request_resource_path: str | None
    allowed_resource_ids: tuple[str, ...]
    resource_id_prefix_to_strip: str | None
    resource_query_parameter: str | None
    default_resource_id: str | None
    fixed_resource_id: str | None
    resource_variant: dict[str, str] | None
    query_any: tuple[dict[str, str], ...]
    query_all: tuple[dict[str, str], ...]
    quantity_multiplier_path: str | None
    quantity_multiplier_query_parameter: str | None
    quantity_multiplier_query_parameter_count: str | None
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


def _resolve_case_insensitive_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        matching_keys = [
            key for key in current if isinstance(key, str) and key.lower() == part.lower()
        ]
        if len(matching_keys) != 1:
            return None
        current = current[matching_keys[0]]
    return current


def _resolve_character_count_path(
    value: Any,
    path: str,
    *,
    case_insensitive: bool,
) -> Any:
    resolver = _resolve_case_insensitive_path if case_insensitive else _resolve_path
    if isinstance(value, list):
        resolved = [resolver(item, path) for item in value]
        return None if any(item is None for item in resolved) else resolved
    return resolver(value, path)


def _text_character_count(value: str, encoding: str) -> int:
    if encoding == "utf16_code_units":
        return len(value.encode("utf-16-le", errors="surrogatepass")) // 2
    return len(value)


def _character_count(
    value: Any,
    encoding: str = "unicode_code_points",
) -> int | None:
    if isinstance(value, str):
        return _text_character_count(value, encoding)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sum(_text_character_count(item, encoding) for item in value)
    return None


def _query_value_is_truthy(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _query_predicate_matches(
    query: dict[str, list[str]],
    predicate: dict[str, str],
) -> bool:
    parameter = predicate["parameter"]
    values = query.get(parameter, [])
    operator = predicate["operator"]
    if operator == "present":
        return parameter in query
    if operator == "truthy":
        return any(_query_value_is_truthy(value) for value in values)
    if operator == "all_non_empty":
        return bool(values) and all(bool(value.strip()) for value in values)
    if operator == "equals":
        return len(values) == 1 and values[0] == predicate["value"]
    return operator == "absent_or_equals" and (
        parameter not in query or (len(values) == 1 and values[0] == predicate["value"])
    )


def _valid_query_predicate(predicate: Any) -> bool:
    if (
        not isinstance(predicate, dict)
        or not isinstance(predicate.get("parameter"), str)
        or not predicate["parameter"]
    ):
        return False
    operator = predicate.get("operator")
    if operator in {"present", "truthy", "all_non_empty"}:
        return set(predicate) == {"parameter", "operator"}
    return (
        operator in {"equals", "absent_or_equals"}
        and set(predicate) == {"parameter", "operator", "value"}
        and isinstance(predicate.get("value"), str)
        and bool(predicate["value"])
    )


def _domain_matches(
    hostname: str | None,
    domains: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> bool:
    if hostname is None:
        return False
    return hostname in domains or any(hostname.endswith(f".{suffix}") for suffix in suffixes)


def _valid_domain_suffix(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 253:
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(
        1 <= len(label) <= 63
        and label[0] in _DOMAIN_EDGE_CHARS
        and label[-1] in _DOMAIN_EDGE_CHARS
        and all(character in _DOMAIN_LABEL_CHARS for character in label)
        for label in labels
    )


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
    operator = predicate.get("operator")
    if not isinstance(operator, str):
        return False
    resolved = _resolve_path(value, predicate["path"])
    if resolved is None:
        return operator.startswith("absent_or_")
    if operator == "not_equals":
        return bool(resolved != predicate["value"])
    if operator == "string_not_contains":
        return isinstance(resolved, str) and predicate["value"] not in resolved
    if operator == "absent_or_false_or_null":
        return resolved is False
    return (
        operator == "absent_or_lte"
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
    if predicate.get("operator") in {"not_equals", "string_not_contains"}:
        return (
            set(predicate) == {"path", "operator", "value"}
            and isinstance(value, str)
            and bool(value)
        )
    return (
        predicate.get("operator") == "absent_or_lte"
        and set(predicate) == {"path", "operator", "value"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


def _request_header_predicate_matches(
    request_header_names: frozenset[str], predicate: dict[str, str]
) -> bool:
    present = predicate["name"] in request_header_names
    return present if predicate["operator"] == "present" else not present


def _valid_request_header_predicate(predicate: Any) -> bool:
    if not isinstance(predicate, dict) or set(predicate) != {"name", "operator"}:
        return False
    name = predicate.get("name")
    return (
        isinstance(name, str)
        and bool(name)
        and name == name.lower()
        and all(character in _HEADER_NAME_CHARS for character in name)
        and predicate.get("operator") in {"present", "absent"}
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
            domain_suffixes = definition.get("domain_suffixes", [])
            endpoints = definition.get("endpoints")
            optional_string_fields = (
                "resource_path",
                "request_resource_path",
                "request_character_count_path",
                "request_character_count_query_parameter",
                "resource_id_prefix_to_strip",
                "minimum_quantity",
                "response_quantity_header",
                "fixed_quantity",
                "resource_query_parameter",
                "default_resource_id",
                "fixed_resource_id",
                "quantity_multiplier_path",
                "quantity_multiplier_query_parameter",
                "quantity_multiplier_query_parameter_count",
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
            request_header_all = definition.get("request_header_all", [])
            request_character_count_path = definition.get("request_character_count_path")
            request_character_count_query_parameter = definition.get(
                "request_character_count_query_parameter"
            )
            request_character_count_case_insensitive = definition.get(
                "request_character_count_case_insensitive", False
            )
            character_count_encoding = definition.get(
                "character_count_encoding", "unicode_code_points"
            )
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
                or not isinstance(domain_suffixes, list)
                or ("domain_suffixes" in definition and not domain_suffixes)
                or not all(_valid_domain_suffix(item) for item in domain_suffixes)
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
                        request_character_count_path
                        or request_character_count_query_parameter,
                        fixed_quantity,
                    )
                )
                != 1
                or fixed_quantity not in {None, "1"}
                or minimum_quantity not in {None, "1"}
                or (
                    minimum_quantity is not None
                    and request_character_count_path is None
                    and request_character_count_query_parameter is None
                )
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
                or character_count_encoding not in {"unicode_code_points", "utf16_code_units"}
                or (
                    "character_count_encoding" in definition
                    and request_character_count_path is None
                    and request_character_count_query_parameter is None
                )
                or (
                    "request_character_count_case_insensitive" in definition
                    and request_character_count_case_insensitive is not True
                )
                or (
                    request_character_count_case_insensitive
                    and request_character_count_path is None
                )
                or (
                    "quantity_multiplier_query_parameter_count" in definition
                    and (
                        request_character_count_path is None
                        and request_character_count_query_parameter is None
                    )
                )
                or (
                    "quantity_multiplier_query_parameter_count" in definition
                    and "quantity_multiplier_path" in definition
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
            if (
                not isinstance(request_header_all, list)
                or ("request_header_all" in definition and not request_header_all)
                or not all(
                    _valid_request_header_predicate(item) for item in request_header_all
                )
            ):
                raise ValueError(
                    "usage observer manifest contains an invalid request-header predicate"
                )
            query_any = definition.get("query_any", [])
            query_all = definition.get("query_all", [])
            if (
                not isinstance(query_any, list)
                or ("query_any" in definition and not query_any)
                or not all(_valid_query_predicate(item) for item in query_any)
                or not isinstance(query_all, list)
                or ("query_all" in definition and not query_all)
                or not all(_valid_query_predicate(item) for item in query_all)
            ):
                raise ValueError("usage observer manifest contains an invalid query predicate")
            multiplier_parameter = definition.get("quantity_multiplier_query_parameter_count")
            if multiplier_parameter is not None and not any(
                predicate.get("parameter") == multiplier_parameter
                and predicate.get("operator") == "all_non_empty"
                for predicate in query_all
            ):
                raise ValueError("query-count multipliers require an all_non_empty predicate")
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
                    domain_suffixes=tuple(domain_suffixes),
                    endpoints=tuple(endpoints),
                    endpoint_match=definition.get("endpoint_match", "prefix"),
                    response_path=response_path,
                    response_quantity_header=response_quantity_header,
                    response_all=tuple(response_all),
                    request_all=tuple(request_all),
                    request_header_all=tuple(request_header_all),
                    request_character_count_path=request_character_count_path,
                    request_character_count_query_parameter=(
                        request_character_count_query_parameter
                    ),
                    request_character_count_case_insensitive=(
                        request_character_count_case_insensitive
                    ),
                    character_count_encoding=character_count_encoding,
                    minimum_quantity=minimum_quantity,
                    fixed_quantity=fixed_quantity,
                    usage_metric=definition["usage_metric"],
                    resource_type=definition.get("resource_type"),
                    resource_path=definition.get("resource_path"),
                    request_resource_path=definition.get("request_resource_path"),
                    allowed_resource_ids=tuple(allowed_resource_ids),
                    resource_id_prefix_to_strip=definition.get(
                        "resource_id_prefix_to_strip"
                    ),
                    resource_query_parameter=definition.get("resource_query_parameter"),
                    default_resource_id=definition.get("default_resource_id"),
                    fixed_resource_id=definition.get("fixed_resource_id"),
                    resource_variant=resource_variant,
                    query_any=tuple(query_any),
                    query_all=tuple(query_all),
                    quantity_multiplier_path=definition.get("quantity_multiplier_path"),
                    quantity_multiplier_query_parameter=definition.get(
                        "quantity_multiplier_query_parameter"
                    ),
                    quantity_multiplier_query_parameter_count=multiplier_parameter,
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
            if _domain_matches(parsed.hostname, candidate.domains, candidate.domain_suffixes)
            and any(
                parsed.path == endpoint or (
                    candidate.endpoint_match == "prefix"
                    and (endpoint == "/" or parsed.path.startswith(f"{endpoint}/"))
                )
                for endpoint in candidate.endpoints
            )
            and (
                not candidate.query_any
                or any(
                    _query_predicate_matches(query, predicate)
                    for predicate in candidate.query_any
                )
            )
            and all(
                _query_predicate_matches(query, predicate) for predicate in candidate.query_all
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
            _domain_matches(parsed.hostname, candidate.domains, candidate.domain_suffixes)
            and any(
                parsed.path == endpoint or parsed.path.startswith(f"{endpoint}/")
                or endpoint == "/"
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

    def needs_response_body(self, url: str) -> bool:
        matched = self._lookup(url)
        return bool(
            matched
            and any(
                item.response_path
                or item.resource_path
                or item.record_id_path
                or item.response_all
                or item.quantity_multiplier_path
                for item in matched[1]
            )
        )

    def observe(
        self,
        url: str,
        response_headers: dict[str, str],
        response_body: dict[str, Any] | None,
        request_body: dict[str, Any] | list[Any] | None = None,
        request_header_names: tuple[str, ...] | list[str] = (),
    ) -> list[ServiceUsageObservation]:
        matched = self._lookup(url)
        if matched is None:
            return []
        parsed, observers = matched
        query = parse_qs(parsed.query, keep_blank_values=True)
        observations: list[ServiceUsageObservation] = []
        normalized_request_header_names = frozenset(
            name.lower() for name in request_header_names
        )
        for observer in observers:
            if not all(
                _request_predicate_matches(request_body, predicate)
                for predicate in observer.request_all
            ):
                continue
            if not all(
                _request_header_predicate_matches(
                    normalized_request_header_names, predicate
                )
                for predicate in observer.request_header_all
            ):
                continue
            if not all(
                _response_predicate_matches(response_body, predicate)
                for predicate in observer.response_all
            ):
                continue
            if (
                observer.request_character_count_path
                or observer.request_character_count_query_parameter
            ):
                character_count = (
                    _character_count(
                        _resolve_character_count_path(
                            request_body,
                            observer.request_character_count_path,
                            case_insensitive=(observer.request_character_count_case_insensitive),
                        ),
                        observer.character_count_encoding,
                    )
                    if observer.request_character_count_path
                    else None
                )
                if character_count is None and observer.request_character_count_query_parameter:
                    character_count = _character_count(
                        query.get(observer.request_character_count_query_parameter),
                        observer.character_count_encoding,
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
            if observer.quantity_multiplier_query_parameter_count:
                query_multiplier = len(
                    query.get(observer.quantity_multiplier_query_parameter_count, [])
                )
                if query_multiplier <= 0:
                    continue
                quantity *= query_multiplier
            if observer.quantity_multiplier_path and (
                observer.quantity_multiplier_query_parameter is None
                or any(
                    _query_value_is_truthy(value)
                    for value in query.get(observer.quantity_multiplier_query_parameter, [])
                )
            ):
                try:
                    response_multiplier = Decimal(
                        str(_resolve_path(response_body, observer.quantity_multiplier_path))
                    )
                except (InvalidOperation, ValueError):
                    response_multiplier = Decimal(0)
                if response_multiplier.is_finite() and response_multiplier > 0:
                    quantity *= response_multiplier
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
            if (
                resource_id is not None
                and observer.resource_id_prefix_to_strip
                and resource_id.startswith(observer.resource_id_prefix_to_strip)
            ):
                resource_id = resource_id[len(observer.resource_id_prefix_to_strip) :]
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
