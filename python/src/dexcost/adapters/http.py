"""HTTP cost adapter — automatic cost tracking for HTTP libraries.

Patches ``requests.Session.send``, ``httpx.Client.send``,
``aiohttp.ClientSession._request``, and
``botocore.httpsession.URLLib3Session.send`` using :pypi:`wrapt` to record
``external_cost`` events whenever an HTTP call targets a domain in the
service catalog.

Usage::

    from dexcost.adapters.http import track_http, register_domain_rate

    register_domain_rate("api.example.com", cost_usd="0.01", per="request")
    patched = track_http()  # list of patched library names

Implements US-035.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import wrapt

from dexcost.adapters._netbytes import classify_destination, measure_bytes_from_headers
from dexcost.config import DexcostConfig
from dexcost.context import get_current_task, is_network_event_suppressed
from dexcost.models.event import Event
from dexcost.service_catalog import ServiceCatalog
from dexcost.session import get_session_manager

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_domain_rates: dict[str, dict[str, Any]] = {}
_original_requests_send: Any = None
_original_httpx_send: Any = None
_original_aiohttp_request: Any = None
_original_botocore_send: Any = None
_original_urllib3_urlopen: Any = None
_requests_patched: bool = False
_httpx_patched: bool = False
_aiohttp_patched: bool = False
_botocore_patched: bool = False
_urllib3_patched: bool = False
_catalog: ServiceCatalog | None = None

# Storage backend wired by set_storage(). When set, recorded HTTP cost events
# are persisted durably (and shipped by the SyncWorker) instead of only being
# appended to the in-memory _recorded_events list.
_storage: Any = None

# Thread-local flag to prevent double-counting when urllib3 is called from
# within an already-patched library (requests, botocore).
_in_patched_call = threading.local()

# Maximum response body size to parse (1 MB)
_MAX_BODY_SIZE = 1_000_000

# Active config — wired by set_network_config(); falls back to defaults.
_network_config: DexcostConfig | None = None

# Count of exceptions swallowed by network accounting — surfaced by
# get_network_error_count() so silent capture failure is observable.
_network_error_count = 0
_network_error_lock = threading.Lock()


def set_network_config(config: DexcostConfig | None) -> None:
    """Wire the adapter to the SDK config (thresholds, on/off toggles)."""
    global _network_config
    _network_config = config


def _cfg() -> DexcostConfig:
    """Return the wired network config, or a defaults instance if none set."""
    return _network_config if _network_config is not None else DexcostConfig(storage="local")


def get_network_error_count() -> int:
    """Number of exceptions swallowed by network accounting since reset."""
    return _network_error_count


def reset_network_error_count() -> None:
    """Reset the swallowed-exception counter (tests / `dexcost status`)."""
    global _network_error_count
    with _network_error_lock:
        _network_error_count = 0


# ---------------------------------------------------------------------------
# Domain rate registration (user overrides — take precedence over catalog)
# ---------------------------------------------------------------------------


def register_domain_rate(domain: str, cost_usd: str, per: str = "request") -> None:
    """Register a cost rate for HTTP calls to a domain.

    User-registered rates take precedence over the service catalog.

    Args:
        domain: The hostname to match (e.g. ``"api.example.com"``).
        cost_usd: Cost per unit in USD as a string (e.g. ``"0.005"``).
        per: Unit label (default ``"request"``).
    """
    _domain_rates[domain] = {"cost_usd": Decimal(cost_usd), "per": per}


def get_domain_rates() -> dict[str, dict[str, Any]]:
    """Return a copy of all registered domain rates."""
    return dict(_domain_rates)


def clear_domain_rates() -> None:
    """Remove all registered domain rates."""
    _domain_rates.clear()


# ---------------------------------------------------------------------------
# Catalog management
# ---------------------------------------------------------------------------


def get_catalog() -> ServiceCatalog:
    """Return the module-level service catalog, creating it lazily."""
    global _catalog
    if _catalog is None:
        _catalog = ServiceCatalog()
    return _catalog


def set_catalog(catalog: ServiceCatalog | None) -> None:
    """Replace the module-level catalog (for testing or custom catalogs)."""
    global _catalog
    _catalog = catalog


def set_storage(storage: Any) -> None:
    """Wire the HTTP adapter to a storage backend.

    Once set, every cost event recorded by the adapter is persisted via
    ``storage.insert_event`` — and its auto-created session task via the
    session manager — so the :class:`SyncWorker` ships HTTP costs to the
    Control Layer. ``dexcost.init(track_http=True)`` calls this automatically.
    Pass ``None`` to detach (events then stay in-memory only).
    """
    global _storage
    _storage = storage


def _persist_event(event: Event) -> None:
    """Record a captured cost event.

    Always appended to the in-memory ``_recorded_events`` list (used by tests
    and lightweight setups) and, when a storage backend is wired via
    :func:`set_storage`, also persisted durably so the SyncWorker ships it.
    """
    _recorded_events.append(event)
    if _storage is not None:
        try:
            _storage.insert_event(event)
        except Exception:
            _log.warning("Failed to persist HTTP cost event to storage", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def track_http() -> list[str]:
    """Patch HTTP libraries to auto-record external costs.

    Patches (when available): ``requests``, ``httpx``, ``aiohttp``,
    ``botocore`` (boto3's HTTP transport), ``urllib3``.

    Returns:
        List of library names that were successfully patched.
    """
    global _requests_patched, _httpx_patched, _aiohttp_patched, _botocore_patched, _urllib3_patched
    global _original_requests_send, _original_httpx_send, _original_aiohttp_request, _original_botocore_send, _original_urllib3_urlopen
    patched: list[str] = []

    # Ensure catalog is loaded
    get_catalog()

    # Try requests
    if not _requests_patched:
        try:
            import requests

            _original_requests_send = requests.Session.send
            wrapt.wrap_function_wrapper("requests", "Session.send", _requests_wrapper)
            _requests_patched = True
            patched.append("requests")
        except ImportError:
            _log.debug("requests not installed — skipping HTTP adapter for requests")

    # Try httpx
    if not _httpx_patched:
        try:
            import httpx

            _original_httpx_send = httpx.Client.send
            wrapt.wrap_function_wrapper("httpx", "Client.send", _httpx_wrapper)
            _httpx_patched = True
            patched.append("httpx")
        except ImportError:
            _log.debug("httpx not installed — skipping HTTP adapter for httpx")

    # Try aiohttp
    if not _aiohttp_patched:
        try:
            import aiohttp  # noqa: F401

            _original_aiohttp_request = aiohttp.ClientSession._request
            wrapt.wrap_function_wrapper("aiohttp", "ClientSession._request", _aiohttp_wrapper)
            _aiohttp_patched = True
            patched.append("aiohttp")
        except ImportError:
            _log.debug("aiohttp not installed — skipping HTTP adapter for aiohttp")

    # Try botocore (boto3's HTTP transport)
    if not _botocore_patched:
        try:
            import botocore.httpsession  # noqa: F401

            _original_botocore_send = botocore.httpsession.URLLib3Session.send
            wrapt.wrap_function_wrapper(
                "botocore.httpsession",
                "URLLib3Session.send",
                _botocore_wrapper,
            )
            _botocore_patched = True
            patched.append("botocore")
        except ImportError:
            _log.debug("botocore not installed — skipping HTTP adapter for boto3")

    # Try urllib3 (used directly by Pinecone, Twilio, SendGrid, etc.)
    # Patched LAST so the thread-local guard prevents double-counting with
    # requests/botocore which also use urllib3 internally.
    if not _urllib3_patched:
        try:
            import urllib3  # noqa: F401

            _original_urllib3_urlopen = urllib3.HTTPConnectionPool.urlopen
            wrapt.wrap_function_wrapper(
                "urllib3",
                "HTTPConnectionPool.urlopen",
                _urllib3_wrapper,
            )
            _urllib3_patched = True
            patched.append("urllib3")
        except ImportError:
            _log.debug("urllib3 not installed — skipping HTTP adapter for urllib3")

    return patched


def untrack_http() -> None:
    """Restore original methods for all patched HTTP libraries."""
    global _requests_patched, _httpx_patched, _aiohttp_patched, _botocore_patched, _urllib3_patched
    global _original_requests_send, _original_httpx_send, _original_aiohttp_request, _original_botocore_send, _original_urllib3_urlopen

    if _requests_patched and _original_requests_send is not None:
        try:
            import requests

            requests.Session.send = _original_requests_send
        except ImportError:
            pass
        _requests_patched = False
        _original_requests_send = None

    if _httpx_patched and _original_httpx_send is not None:
        try:
            import httpx

            httpx.Client.send = _original_httpx_send
        except ImportError:
            pass
        _httpx_patched = False
        _original_httpx_send = None

    if _aiohttp_patched and _original_aiohttp_request is not None:
        try:
            import aiohttp

            aiohttp.ClientSession._request = _original_aiohttp_request
        except ImportError:
            pass
        _aiohttp_patched = False
        _original_aiohttp_request = None

    if _botocore_patched and _original_botocore_send is not None:
        try:
            import botocore.httpsession

            botocore.httpsession.URLLib3Session.send = _original_botocore_send
        except ImportError:
            pass
        _botocore_patched = False
        _original_botocore_send = None

    if _urllib3_patched and _original_urllib3_urlopen is not None:
        try:
            import urllib3

            urllib3.HTTPConnectionPool.urlopen = _original_urllib3_urlopen
        except ImportError:
            pass
        _urllib3_patched = False
        _original_urllib3_urlopen = None


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _requests_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``requests.Session.send``."""
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        body = getattr(req, "body", None)
        body_len = len(body) if isinstance(body, (bytes, bytearray, str)) else 0
        headers = {str(k): str(v) for k, v in getattr(req, "headers", {}).items()}
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers=headers, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response


def _httpx_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``httpx.Client.send``."""
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        content = getattr(req, "content", None)
        body_len = len(content) if isinstance(content, (bytes, bytearray)) else 0
        headers = {str(k): str(v) for k, v in getattr(req, "headers", {}).items()}
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers=headers, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response


async def _aiohttp_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``aiohttp.ClientSession._request``.

    All public methods (get, post, put, etc.) funnel through _request.
    Signature: _request(method, str_or_url, ...) -> ClientResponse.
    """
    t0 = time.monotonic()
    response = await wrapped(*args, **kwargs)
    latency_ms = int((time.monotonic() - t0) * 1000)
    method = str(args[0]) if args else str(kwargs.get("method", "GET"))
    url = str(args[1]) if len(args) > 1 else str(kwargs.get("str_or_url", ""))
    # bytes-out is approximate (request-line overhead): no prepared-request object available here
    _handle_http_call(url, method=method, request_headers={},
                      request_body_len=0, response=response, latency_ms=latency_ms)
    return response


def _botocore_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``botocore.httpsession.URLLib3Session.send``.

    Intercepts every AWS SDK HTTP call. The request arg is an
    ``AWSPreparedRequest`` with a ``.url`` attribute.
    """
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        body = getattr(req, "body", None)
        body_len = len(body) if isinstance(body, (bytes, bytearray, str)) else 0
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers={}, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response


def _urllib3_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``urllib3.HTTPConnectionPool.urlopen``.

    Catches HTTP calls from SDKs that use urllib3 directly (Pinecone,
    Twilio, SendGrid, etc.). Skips recording if the call originated
    from an already-patched library (requests, botocore) to prevent
    double-counting.
    """
    # Skip if we're inside a higher-level patched call
    if getattr(_in_patched_call, "active", False):
        return wrapped(*args, **kwargs)

    t0 = time.monotonic()
    response = wrapped(*args, **kwargs)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Reconstruct URL from the pool's scheme/host/port + the request path
    req_method = str(args[0]) if args else str(kwargs.get("method", "GET"))
    url_path = args[1] if len(args) > 1 else kwargs.get("url", "")
    scheme = getattr(instance, "scheme", "https")
    host = getattr(instance, "host", "")
    port = getattr(instance, "port", None)
    if port and port not in (80, 443):
        full_url = f"{scheme}://{host}:{port}{url_path}"
    else:
        full_url = f"{scheme}://{host}{url_path}"
    # bytes-out is approximate (request-line overhead): no prepared-request object available here
    _handle_http_call(full_url, method=req_method, request_headers={},
                      request_body_len=0, response=response, latency_ms=latency_ms)
    return response


# ---------------------------------------------------------------------------
# Response body/header extraction helpers
# ---------------------------------------------------------------------------


def _get_response_headers(response: Any) -> dict[str, str]:
    """Extract headers from a requests or httpx response as a plain dict."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    # Both requests and httpx have dict-like headers
    return {str(k): str(v) for k, v in headers.items()}


def _get_response_body(response: Any) -> dict[str, Any] | None:
    """Try to parse the response body as JSON.

    Returns None if:
    - Content-Type is not application/json
    - Content-Length > 1 MB
    - Parsing fails
    """
    headers = _get_response_headers(response)

    # Check content type
    content_type = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            content_type = v.lower()
            break
    if "application/json" not in content_type:
        return None

    # Check content length
    for k, v in headers.items():
        if k.lower() == "content-length":
            try:
                if int(v) > _MAX_BODY_SIZE:
                    return None
            except (ValueError, TypeError):
                pass
            break

    # Try to parse JSON
    try:
        # For requests.Response, .json() caches the result
        json_method = getattr(response, "json", None)
        if json_method is not None:
            body = json_method()
            if isinstance(body, dict):
                return body
    except Exception:
        pass

    return None


def _response_body_len(response: Any) -> int:
    """Best-effort response body length in bytes.

    Uses the ``Content-Length`` header when present; otherwise falls back to
    the length of an already-materialised body. Never forces a stream read.
    """
    headers = _get_response_headers(response)
    for key, value in headers.items():
        if key.lower() == "content-length":
            try:
                return max(0, int(value))
            except (ValueError, TypeError):
                break
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    return 0


# ---------------------------------------------------------------------------
# Cost recording logic
# ---------------------------------------------------------------------------


def _handle_http_call(
    url: str,
    *,
    method: str = "GET",
    request_headers: dict[str, Any] | None = None,
    request_body_len: int = 0,
    response: Any = None,
    latency_ms: int = 0,
) -> None:
    """Record cost + network bytes for one instrumented HTTP call.

    Fail-silent: any exception is swallowed and counted (see
    get_network_error_count) so a measurement bug never breaks the call.
    """
    try:
        _handle_http_call_inner(
            url, method, request_headers or {}, request_body_len, response, latency_ms
        )
    except Exception:  # broad catch intentional: must never break the caller's HTTP call
        global _network_error_count
        with _network_error_lock:
            _network_error_count += 1
        _log.warning("network capture failed for %s", url, exc_info=True)


def _resolve_task() -> Any | None:
    """Return the active task, or an auto-session task, or None."""
    task = get_current_task()
    if task is not None:
        return task
    session_mgr = get_session_manager()
    return session_mgr.get_or_create_session("http_call", _storage)


def _measure_bytes(
    method: str,
    url: str,
    domain: str,
    protocol: str,
    request_headers: dict[str, Any],
    request_body_len: int,
    response: Any,
    track_network: bool,
) -> tuple[int, int, dict[str, str], dict[str, Any]]:
    """Return (bytes_out, bytes_in, response_headers, byte_details).

    When *track_network* is False, byte measurement is skipped entirely;
    ``byte_details`` is empty so callers don't embed network fields in events.
    ``response_headers`` is still extracted in both cases because the catalog
    path needs it for cost extraction regardless of the toggle.
    """
    response_headers = _get_response_headers(response) if response is not None else {}
    if not track_network:
        return 0, 0, response_headers, {}
    bytes_out = measure_bytes_from_headers(method, url, request_headers, request_body_len)
    response_body_len = _response_body_len(response) if response is not None else 0
    bytes_in = measure_bytes_from_headers("", "", response_headers, response_body_len)
    byte_details: dict[str, Any] = {
        "protocol": protocol,
        "request_bytes": bytes_out,
        "response_bytes": bytes_in,
        "is_internal_traffic": classify_destination(domain),
    }
    return bytes_out, bytes_in, response_headers, byte_details


def _handle_domain_rate(
    url: str, domain: str,
    track_network: bool, bytes_in: int, bytes_out: int,
    byte_details: dict[str, Any],
) -> bool:
    """Handle user-registered domain-rate path. Returns True if handled."""
    rate = _domain_rates.get(domain)
    if rate is None:
        return False
    task = _resolve_task()
    if task is None:
        # Domain is ours (registered rate) but no resolvable task exists — silently
        # swallow the call.  Consistent with the no-active-task no-op rule; no
        # orphan rows are created.
        return True
    if track_network:
        task._network.record(
            domain, bytes_in=bytes_in, bytes_out=bytes_out,
            is_internal=byte_details.get("is_internal_traffic"),
        )
    event = Event(
        task_id=task.task_id, event_type="external_cost",
        cost_usd=rate["cost_usd"], cost_confidence="exact",
        pricing_source="rate_registry", service_name=domain,
        details={"url": url, "per": rate["per"], **byte_details},
    )
    _persist_event(event)
    return True


def _handle_catalog_entry(
    url: str, domain: str,
    track_network: bool, bytes_in: int, bytes_out: int,
    response_headers: dict[str, str], response: Any,
    byte_details: dict[str, Any],
) -> bool:
    """Handle service-catalog path. Returns True if handled."""
    catalog = get_catalog()
    entry = catalog.lookup(url)
    if entry is None:
        return False
    task = _resolve_task()
    if task is None:
        # URL matched the catalog (it's a known service) but no resolvable task
        # exists — silently swallow the call.  Consistent with the no-active-task
        # no-op rule; no orphan rows are created.
        return True
    if track_network:
        task._network.record(
            domain, bytes_in=bytes_in, bytes_out=bytes_out,
            is_internal=byte_details.get("is_internal_traffic"),
        )
    result = catalog.extract_cost(
        entry, response_headers, _get_response_body(response) if response else None
    )
    if result is not None:
        event = Event(
            task_id=task.task_id, event_type="external_cost",
            cost_usd=result.amount, cost_confidence=result.confidence,
            pricing_source=result.pricing_source,
            pricing_version=catalog.catalog_version,
            service_name=result.service_name,
            details={"url": url, **byte_details},
        )
    else:
        event = Event(
            task_id=task.task_id, event_type="external_cost",
            cost_usd=Decimal("0"), cost_confidence="unknown",
            pricing_source="service_catalog", service_name=entry.display_name,
            details={"url": url, **byte_details},
        )
    _persist_event(event)
    return True


def _handle_uncataloged(
    url: str, method: str, domain: str,
    bytes_in: int, bytes_out: int, status_code: int, latency_ms: int,
    byte_details: dict[str, Any], cfg: DexcostConfig,
) -> None:
    """Handle un-cataloged path: record bytes and emit network event if notable.

    Precondition: only called when ``track_network`` is True.  The dispatcher
    (_handle_http_call_inner) guards with ``if not track_network: return``
    immediately before invoking this helper, so bytes are recorded
    unconditionally here without a redundant inner guard.
    """
    task = get_current_task()
    if task is None:
        return  # anonymous traffic — never create orphan rows
    task._network.record(
        domain, bytes_in=bytes_in, bytes_out=bytes_out,
        is_internal=byte_details.get("is_internal_traffic"),
    )
    if is_network_event_suppressed():
        return  # the `llm_call` event already represents this call
    notable = (
        (bytes_in + bytes_out) > cfg.network_event_threshold_bytes
        or (cfg.network_event_on_error and status_code >= 400)
        or (cfg.network_event_latency_ms > 0 and latency_ms > cfg.network_event_latency_ms)
    )
    if not notable:
        return  # counters already updated; below threshold → no event

    # v2 §6.4 — emission stamps cost_usd=0 and a cost_pending marker; the real
    # egress cost is back-filled by _aggregate_costs at task finalize.
    event = Event(
        task_id=task.task_id, event_type="network",
        cost_usd=Decimal("0"), cost_confidence="unknown",
        pricing_source=None, service_name=domain,
        details={
            "url": url, "method": method, "status_code": status_code,
            "cost_pending": True,
            **byte_details,
        },
    )
    _persist_event(event)


def _handle_http_call_inner(
    url: str,
    method: str,
    request_headers: dict[str, Any],
    request_body_len: int,
    response: Any,
    latency_ms: int,
) -> None:
    parsed = urlparse(str(url))
    domain = parsed.hostname or ""
    protocol = parsed.scheme or "https"

    cfg = _cfg()
    track_network = cfg.track_network

    bytes_out, bytes_in, response_headers, byte_details = _measure_bytes(
        method, url, domain, protocol,
        request_headers, request_body_len, response, track_network,
    )
    status_code = int(
        getattr(response, "status_code", None)
        or getattr(response, "status", 0)
        or 0
    )

    # ── 1. user-registered domain rate (cataloged — unaffected by toggle) ──
    if _handle_domain_rate(url, domain, track_network, bytes_in, bytes_out, byte_details):
        return

    # ── 2. service-catalog match (cataloged — unaffected by toggle) ────────
    if _handle_catalog_entry(
        url, domain, track_network, bytes_in, bytes_out,
        response_headers, response, byte_details,
    ):
        return

    # ── 3. un-cataloged: skip entirely when track_network=False ────────────
    if not track_network:
        return

    _handle_uncataloged(
        url, method, domain, bytes_in, bytes_out, status_code, latency_ms, byte_details, cfg,
    )


# Module-level list for events recorded by the HTTP adapter.
# In a fully-wired setup, users would use CostTracker which handles storage.
# This list allows tests and simple setups to verify events were recorded.
_recorded_events: list[Event] = []


def get_recorded_events() -> list[Event]:
    """Return all events recorded by the HTTP adapter since last clear."""
    return list(_recorded_events)


def clear_recorded_events() -> None:
    """Clear the recorded events list."""
    _recorded_events.clear()
