"""Service catalog for automatic non-LLM cost extraction.

Loads service_prices.json (bundled or remote) and provides:
- Domain -> service entry lookup
- Cost extraction from HTTP response headers/body
- Remote catalog refresh from control layer
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_log = logging.getLogger(__name__)
SUPPORTED_SAFETY_POLICY_VERSION = "2026-07-14.2"

_DEFAULT_DATA_PATH = Path(__file__).parent / "data" / "service_prices.json"


class _NoCatalogRedirects(urllib.request.HTTPRedirectHandler):
    """Reject redirects so catalog credentials never cross an origin boundary."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_catalog_request(
    request: urllib.request.Request, timeout: int
) -> Any:
    return urllib.request.build_opener(_NoCatalogRedirects()).open(request, timeout=timeout)


@dataclass
class ServiceEntry:
    """A single service entry from the catalog."""

    key: str
    display_name: str
    domains: list[str]
    category: str
    pricing_model: str
    cost_extraction: dict[str, Any]
    source: str
    last_verified: str
    endpoints: list[str] | None = None
    # Pricing fields (varying names in JSON — stored generically)
    rate_fields: dict[str, Any] | None = None
    note: str | None = None


@dataclass
class CostExtractionResult:
    """Result of extracting cost from an HTTP response."""

    amount: Decimal
    confidence: str
    service_name: str
    pricing_source: str


class ServiceCatalog:
    """Loads and queries the bundled service price catalog."""

    def __init__(self, data_path: Path | None = None) -> None:
        self._data_path = data_path or _DEFAULT_DATA_PATH
        self._entries: dict[str, ServiceEntry] = {}
        self._overrides: dict[str, dict[str, Any]] = {}
        self._raw_data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load the JSON catalog from disk."""
        try:
            with open(self._data_path) as f:
                data: dict[str, Any] = json.load(f)
            entries = self._parse_catalog_entries(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to load service catalog from %s: %s", self._data_path, exc)
            data = {}
            entries = {}
        except (KeyError, TypeError, ValueError, decimal.InvalidOperation) as exc:
            _log.warning("Invalid service catalog at %s: %s", self._data_path, exc)
            data = {}
            entries = {}
        self._raw_data = data
        self._entries = entries

    @staticmethod
    def _parse_entry(key: str, data: dict[str, Any]) -> ServiceEntry:
        """Parse a single JSON entry into a ServiceEntry."""
        # Collect all rate/cost fields that aren't standard fields
        standard_keys = {
            "display_name", "domains", "category", "pricing_model",
            "cost_extraction", "source", "last_verified", "endpoints", "note",
        }
        rate_fields = {k: v for k, v in data.items() if k not in standard_keys}

        return ServiceEntry(
            key=key,
            display_name=data["display_name"],
            domains=data["domains"],
            category=data["category"],
            pricing_model=data["pricing_model"],
            cost_extraction=data["cost_extraction"],
            source=data["source"],
            last_verified=data["last_verified"],
            endpoints=data.get("endpoints"),
            rate_fields=rate_fields if rate_fields else None,
            note=data.get("note"),
        )

    @classmethod
    def _parse_catalog_entries(cls, data: dict[str, Any]) -> dict[str, ServiceEntry]:
        if not isinstance(data, dict):
            raise ValueError("catalog data must be an object")

        entries: dict[str, ServiceEntry] = {}
        for key, entry_data in data.items():
            if key == "_meta":
                continue
            if not key or not isinstance(entry_data, dict):
                raise ValueError(f"catalog entry {key} must be an object")
            for field in (
                "display_name",
                "category",
                "pricing_model",
                "source",
                "last_verified",
            ):
                if not isinstance(entry_data.get(field), str) or not entry_data[field]:
                    raise ValueError(f"catalog entry {key} has invalid {field}")
            domains = entry_data.get("domains")
            if not isinstance(domains, list) or not domains or not all(
                isinstance(domain, str) and domain for domain in domains
            ):
                raise ValueError(f"catalog entry {key} has invalid domains")
            endpoints = entry_data.get("endpoints")
            if endpoints is not None and (
                not isinstance(endpoints, list)
                or not all(isinstance(endpoint, str) and endpoint for endpoint in endpoints)
            ):
                raise ValueError(f"catalog entry {key} has invalid endpoints")
            extraction = entry_data.get("cost_extraction")
            extraction_type = extraction.get("type") if isinstance(extraction, dict) else None
            if extraction_type not in {
                "response_body",
                "response_header",
                "endpoint_match",
                "fixed",
            }:
                raise ValueError(f"catalog entry {key} has invalid extraction type")
            transform = extraction.get("transform")
            if transform is not None and (
                extraction_type != "response_body"
                or transform not in {"ms_to_seconds", "ms_to_minutes"}
            ):
                raise ValueError(f"catalog entry {key} has unsupported transform")
            if extraction_type == "response_body" and (
                not isinstance(extraction.get("path"), str) or not extraction["path"]
            ):
                raise ValueError(f"catalog entry {key} has invalid response path")
            if extraction_type == "response_header" and (
                not isinstance(extraction.get("header"), str) or not extraction["header"]
            ):
                raise ValueError(f"catalog entry {key} has invalid response header")
            if extraction_type == "endpoint_match" and not endpoints:
                raise ValueError(f"catalog entry {key} has no endpoint predicate")
            positive_rate_count = 0
            for field, value in entry_data.items():
                if field.startswith("cost_per_"):
                    if not field.endswith("_usd"):
                        raise ValueError(f"catalog entry {key} has invalid rate field {field}")
                    rate = Decimal(str(value))
                    if not rate.is_finite() or rate <= 0:
                        raise ValueError(f"catalog entry {key} has unsafe rate {field}={value}")
                    positive_rate_count += 1
            if positive_rate_count != 1:
                raise ValueError(f"catalog entry {key} must have exactly one positive rate")
            entry = cls._parse_entry(key, entry_data)
            entries[key] = entry

        if not entries:
            raise ValueError("catalog must contain at least one service entry")
        return entries

    @classmethod
    def _parse_remote_envelope(
        cls, payload: Any
    ) -> tuple[dict[str, Any], dict[str, ServiceEntry]]:
        if not isinstance(payload, dict):
            raise ValueError("catalog response must be an object")
        data = payload.get("data")
        meta = payload.get("meta")
        if not isinstance(data, dict) or not isinstance(meta, dict):
            raise ValueError("catalog response requires data and meta objects")

        entries = cls._parse_catalog_entries(data)
        service_count = meta.get("service_count")
        disabled_count = meta.get("disabled_service_count")
        disabled_entries = meta.get("disabled_entries")
        if (
            not isinstance(meta.get("catalog_version"), str)
            or not meta["catalog_version"]
            or not isinstance(meta.get("safety_policy_version"), str)
            or meta["safety_policy_version"] != SUPPORTED_SAFETY_POLICY_VERSION
            or not isinstance(meta.get("source"), str)
            or not meta["source"]
            or isinstance(service_count, bool)
            or not isinstance(service_count, int)
            or service_count != len(entries)
            or isinstance(disabled_count, bool)
            or not isinstance(disabled_count, int)
            or not isinstance(disabled_entries, list)
            or disabled_count != len(disabled_entries)
        ):
            raise ValueError("catalog response metadata is inconsistent")

        disabled_keys: set[str] = set()
        for item in disabled_entries:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("service_key"), str)
                or not item["service_key"]
            ):
                raise ValueError("disabled catalog entry is malformed")
            disabled_keys.add(item["service_key"])
        if len(disabled_keys) != len(disabled_entries) or disabled_keys.intersection(entries):
            raise ValueError("disabled catalog entries are inconsistent")

        data_meta = data.get("_meta")
        if (
            not isinstance(data_meta, dict)
            or data_meta.get("version") != meta["catalog_version"]
            or data_meta.get("service_count") != service_count
            or data_meta.get("disabled_service_count") != disabled_count
            or data_meta.get("safety_policy_version") != meta["safety_policy_version"]
        ):
            raise ValueError("catalog data metadata is inconsistent")
        return data, entries

    def lookup(self, url: str) -> ServiceEntry | None:
        """Match a URL against the catalog by domain and endpoint.

        Wildcard domains like ``*.pinecone.io`` are supported via fnmatch.
        When multiple entries share the same domain (e.g. Google Maps),
        endpoint matching is used to disambiguate.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        # Check overrides first (exact domain match only)
        if hostname in self._overrides:
            # Overrides are handled at extraction time, but we still need
            # to find the ServiceEntry for metadata.
            pass

        # Collect all entries whose domains match
        candidates: list[ServiceEntry] = []
        for entry in self._entries.values():
            if self._domain_matches(hostname, entry.domains):
                candidates.append(entry)

        if not candidates:
            return None

        # If only one candidate, return it (no endpoint filtering needed)
        if len(candidates) == 1:
            return candidates[0]

        # Multiple candidates: filter by endpoint match
        for entry in candidates:
            if entry.endpoints:
                for ep in entry.endpoints:
                    if path.startswith(ep):
                        return entry

        # Fallback: return first candidate without endpoints requirement
        for entry in candidates:
            if not entry.endpoints:
                return entry

        # Last resort: first candidate
        return candidates[0]

    @staticmethod
    def _domain_matches(hostname: str, patterns: list[str]) -> bool:
        """Check if hostname matches any of the domain patterns."""
        for pattern in patterns:
            if pattern.startswith("*."):
                # Wildcard: *.pinecone.io should match
                # "my-index.svc.us-east1-gcp.pinecone.io"
                suffix = pattern[1:]  # ".pinecone.io"
                if hostname.endswith(suffix) or hostname == pattern[2:]:
                    return True
            else:
                if hostname == pattern:
                    return True
        return False

    def extract_cost(
        self,
        entry: ServiceEntry,
        response_headers: dict[str, str],
        response_body: dict[str, Any] | None,
    ) -> CostExtractionResult | None:
        """Apply extraction rules to get cost from HTTP response.

        Returns None if cost cannot be extracted.
        """
        # Check user override first
        override = self._overrides.get(entry.key)
        if override:
            return CostExtractionResult(
                amount=override["cost_per_unit"],
                confidence="exact",
                service_name=entry.display_name,
                pricing_source="user_override",
            )

        extraction = entry.cost_extraction
        ext_type = extraction.get("type", "fixed")

        if ext_type == "response_body":
            return self._extract_from_body(entry, extraction, response_body)
        elif ext_type == "response_header":
            return self._extract_from_header(entry, extraction, response_headers)
        elif ext_type == "endpoint_match":
            return self._extract_endpoint_match(entry)
        elif ext_type == "fixed":
            return self._extract_fixed(entry)
        else:
            _log.warning("Unknown extraction type %r for %s", ext_type, entry.key)
            return None

    def _extract_from_body(
        self,
        entry: ServiceEntry,
        extraction: dict[str, Any],
        response_body: dict[str, Any] | None,
    ) -> CostExtractionResult | None:
        """Extract cost from a response body field."""
        if response_body is None:
            # Use fallback credits if available
            fallback = extraction.get("fallback_credits")
            if fallback is not None:
                rate = self._get_rate(entry)
                if rate is not None:
                    amount = Decimal(str(fallback)) * rate
                    return CostExtractionResult(
                        amount=amount,
                        confidence="estimated",
                        service_name=entry.display_name,
                        pricing_source="service_catalog",
                    )
            return None

        path = extraction.get("path", "")
        value = self._resolve_dotted_path(response_body, path)
        if value is None:
            # Try fallback
            fallback = extraction.get("fallback_credits")
            if fallback is not None:
                rate = self._get_rate(entry)
                if rate is not None:
                    amount = Decimal(str(fallback)) * rate
                    return CostExtractionResult(
                        amount=amount,
                        confidence="estimated",
                        service_name=entry.display_name,
                        pricing_source="service_catalog",
                    )
            return None

        try:
            raw_value = Decimal(str(value))
        except (decimal.InvalidOperation, ValueError):
            return None

        # Apply transform if present
        transform = extraction.get("transform")
        if transform:
            raw_value = self._apply_transform(transform, raw_value, entry)
            confidence = "computed"
        else:
            # Multiply by rate
            rate = self._get_rate(entry)
            if rate is not None:
                raw_value = raw_value * rate
            confidence = "computed"

        return CostExtractionResult(
            amount=raw_value,
            confidence=confidence,
            service_name=entry.display_name,
            pricing_source="service_catalog",
        )

    def _extract_from_header(
        self,
        entry: ServiceEntry,
        extraction: dict[str, Any],
        response_headers: dict[str, str],
    ) -> CostExtractionResult | None:
        """Extract cost from a response header."""
        header = extraction.get("header", "")

        # Case-insensitive header lookup
        header_value: str | None = None
        for k, v in response_headers.items():
            if k.lower() == header.lower():
                header_value = v
                break

        if header_value is None:
            return None

        try:
            raw_value = Decimal(str(header_value))
        except (decimal.InvalidOperation, ValueError):
            return None
        rate = self._get_rate(entry)
        if rate is not None:
            raw_value = raw_value * rate

        return CostExtractionResult(
            amount=raw_value,
            confidence="computed",
            service_name=entry.display_name,
            pricing_source="service_catalog",
        )

    def _extract_endpoint_match(self, entry: ServiceEntry) -> CostExtractionResult | None:
        """Fixed cost per request from endpoint match."""
        cost = self._get_fixed_cost(entry)
        if cost is None:
            return None
        return CostExtractionResult(
            amount=cost,
            confidence="exact",
            service_name=entry.display_name,
            pricing_source="service_catalog",
        )

    def _extract_fixed(self, entry: ServiceEntry) -> CostExtractionResult | None:
        """Fixed cost per request."""
        cost = self._get_fixed_cost(entry)
        if cost is None:
            return None
        return CostExtractionResult(
            amount=cost,
            confidence="exact",
            service_name=entry.display_name,
            pricing_source="service_catalog",
        )

    @staticmethod
    def _get_rate(entry: ServiceEntry) -> Decimal | None:
        """Get the per-unit rate from the entry's rate fields."""
        if not entry.rate_fields:
            return None
        # Look for cost_per_* fields
        for k, v in entry.rate_fields.items():
            if k.startswith("cost_per_") and k.endswith("_usd"):
                return Decimal(str(v))
        return None

    @staticmethod
    def _get_fixed_cost(entry: ServiceEntry) -> Decimal | None:
        """Get the fixed cost per request from rate fields."""
        if not entry.rate_fields:
            return None
        for k, v in entry.rate_fields.items():
            if k.startswith("cost_per_") and k.endswith("_usd"):
                return Decimal(str(v))
        return None

    @staticmethod
    def _resolve_dotted_path(data: dict[str, Any], path: str) -> Any:
        """Resolve a dotted path like 'data.stats.computeUnits' in a dict."""
        parts = path.split(".")
        current: Any = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _apply_transform(
        self, transform: str, raw_value: Decimal, entry: ServiceEntry
    ) -> Decimal:
        """Apply a named transform to a raw value."""
        if transform == "ms_to_seconds":
            seconds = raw_value / Decimal("1000")
            rate = self._get_rate(entry)
            return (seconds * rate) if rate else Decimal("0")
        elif transform == "ms_to_minutes":
            minutes = raw_value / Decimal("60000")
            rate = self._get_rate(entry)
            return (minutes * rate) if rate else Decimal("0")
        elif transform == "stripe_fee":
            # amount is in cents
            amount_dollars = raw_value / Decimal("100")
            return amount_dollars * Decimal("0.029") + Decimal("0.30")
        else:
            _log.warning("Unknown transform %r", transform)
            return raw_value

    def register_override(
        self, service_key: str, cost_per_unit: Decimal, per: str = "request"
    ) -> None:
        """Register a user override for a service entry.

        Takes precedence over catalog rates during extraction.
        """
        self._overrides[service_key] = {
            "cost_per_unit": cost_per_unit,
            "per": per,
        }

    def refresh_from_url(self, url: str, api_key: str | None = None) -> bool:
        """Atomically replace the catalog from a conformant remote envelope."""
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            request = urllib.request.Request(url, headers=headers)
            with _open_catalog_request(request, timeout=10) as resp:
                payload: Any = json.loads(resp.read().decode())
            remote_data, entries = self._parse_remote_envelope(payload)
        except Exception:
            _log.warning("Failed to refresh catalog from %s", url, exc_info=True)
            return False

        self._raw_data = remote_data
        self._entries = entries
        return True

    @property
    def catalog_version(self) -> str:
        """Return a hash of the loaded data for pricing_version tracking."""
        content = json.dumps(self._raw_data, sort_keys=True)
        override_content = json.dumps(
            {k: {"cost_per_unit": str(v["cost_per_unit"]), "per": v["per"]}
             for k, v in self._overrides.items()},
            sort_keys=True,
        )
        combined = content + override_content
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @property
    def entries(self) -> dict[str, ServiceEntry]:
        """Return a copy of all loaded entries."""
        return dict(self._entries)
