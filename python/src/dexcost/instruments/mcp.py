"""Auto-instrumentation for MCP (Model Context Protocol) tool calls.

Monkey-patches ``mcp.ClientSession.call_tool`` using :pypi:`wrapt` so that
every MCP tool invocation inside an active :class:`~dexcost.tracker.CostTracker`
task is automatically recorded as an ``external_cost`` event.

Usage::

    from dexcost import CostTracker, instrument_mcp

    tracker = CostTracker()
    instrument_mcp(tracker)

    # All subsequent MCP call_tool() invocations inside a
    # tracked task are captured automatically.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import (
    apply_event_capability,
    default_tool_capability,
    get_capability,
)
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.idempotency import (
    IdempotencyKey,
    apply_event_idempotency,
    capture_idempotency_key,
)
from dexcost.instruments._errors import error_type_of, finalize_failed_auto_task
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ``operation.error.type`` for a tool that returned a protocol-level error
# (``CallToolResult.isError``) rather than raising.
TOOL_ERROR_TYPE = "tool_error"

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}
_patched_owner: Any | None = None

_CALL_RATE_UNITS = frozenset(
    {"call", "calls", "invocation", "invocations", "request", "requests"}
)
_CREDIT_RATE_UNITS = frozenset({"credit", "credits", "credit_count"})
_BILLING_JSON_LIMIT_BYTES = 1_000_000

# MCP service aliases are distributed in the signed service catalog. There is no
# code fallback: a new or renamed tool can be attributed immediately, while money
# remains unknown until the control plane publishes a reviewed alias or the caller
# registers an explicit ``mcp:<tool_name>`` rate.

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_mcp(tracker: Any) -> None:
    """Monkey-patch the MCP SDK to capture tool calls automatically.

    Patches ``mcp.client.session.ClientSession.call_tool`` (async).

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            look up rates and persist events.

    Raises:
        ImportError: If the ``mcp`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched, _patched_owner

    try:
        import mcp.client.session as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required for MCP auto-instrumentation. "
            "Install it with: pip install mcp"
        ) from exc

    from mcp.client.session import ClientSession

    if _patched:
        if _patched_owner is ClientSession:
            raise RuntimeError(
                "MCP instrumentation is already active. "
                "Call uninstrument_mcp() before re-instrumenting."
            )
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None

    _active_tracker = tracker
    _patched_owner = ClientSession

    _originals["call_tool"] = ClientSession.call_tool

    wrapt.wrap_function_wrapper(
        "mcp.client.session",
        "ClientSession.call_tool",
        _call_tool_wrapper,
    )

    _patched = True


def uninstrument_mcp() -> None:
    """Remove MCP monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched, _patched_owner

    if not _patched:
        return

    try:
        from mcp.client.session import ClientSession
    except ImportError:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    if _patched_owner is not ClientSession:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    if "call_tool" in _originals:
        ClientSession.call_tool = _originals["call_tool"]  # type: ignore[method-assign]

    _originals.clear()
    _active_tracker = None
    _patched = False
    _patched_owner = None


# ---------------------------------------------------------------------------
# Wrapper function
# ---------------------------------------------------------------------------


def _call_tool_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``ClientSession.call_tool`` (async)."""
    # Capture caller-owned context when the operation is invoked, not later
    # when its coroutine happens to be awaited.
    tool_name = _tool_name(args, kwargs)
    capability = get_capability() or default_tool_capability(tool_name)
    idempotency_key = capture_idempotency_key()
    task = get_current_task()
    # call_tool is always async in the MCP SDK, return the coroutine.
    return _async_call_tool_handler(
        wrapped,
        instance,
        args,
        kwargs,
        task,
        tool_name,
        capability,
        idempotency_key,
    )


def _tool_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    raw = args[0] if args else kwargs.get("name", "unknown")
    value = str(raw).strip()
    return (value or "unknown")[:256]


async def _async_call_tool_handler(
    wrapped: Any,
    instance: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    task: Any,
    tool_name: str,
    capability: CapabilityIdentity,
    idempotency_key: IdempotencyKey | None,
) -> Any:
    """Async handler that records an MCP tool call as an external_cost event."""
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("mcp.tool_call")
        task = auto_task_obj
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        is_error = False
        event: Event | None = None

        try:
            with suppress_network_event():
                result = await wrapped(*args, **kwargs)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            try:
                event = _record_tool_call(
                    tool_name=tool_name,
                    instance=instance,
                    latency_ms=latency_ms,
                    is_error=True,
                    error_type=error_type_of(exc),
                    task=task,
                    capability=capability,
                    idempotency_key=idempotency_key,
                )
            except Exception:
                _log.debug("dexcost: failed to record MCP error event", exc_info=True)
            if auto:
                finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
            raise

        latency_ms = int((time.perf_counter() - start_time) * 1000)

        # Check if the MCP result itself signals an error. The MCP SDK spells
        # it ``isError``; ``is_error`` is accepted for other client shapes.
        is_error = _result_is_error(result)

        try:
            event = _record_tool_call(
                tool_name=tool_name,
                instance=instance,
                latency_ms=latency_ms,
                is_error=is_error,
                error_type=TOOL_ERROR_TYPE if is_error else None,
                task=task,
                capability=capability,
                idempotency_key=idempotency_key,
                result=result,
            )
        except Exception:
            _log.debug("dexcost: failed to record MCP event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            if is_error:
                finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
            else:
                try:
                    finalize_auto_task(auto_task_obj, event, status="success")
                    if _active_tracker is not None:
                        _active_tracker._storage.insert_task(auto_task_obj)
                except Exception:
                    _log.debug("dexcost: failed to finalize MCP auto-task", exc_info=True)

        return result
    except Exception:
        if auto and auto_task_obj is not None:
            _log.debug("dexcost: MCP auto-task call failed", exc_info=True)
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Cost resolution
# ---------------------------------------------------------------------------


def _resolve_cost(
    tool_name: str,
    quantities: Mapping[str, Decimal],
) -> tuple[Decimal, str, str, str | None]:
    """Resolve cost for an MCP tool call.

    Resolution order:
    1. Rate registry: ``"mcp:<tool_name>"``
    2. Service catalog mapping: tool_name -> catalog key -> rate registry
    3. Fallback: cost=0, confidence="unknown"

    Returns:
        ``(cost_usd, cost_confidence, pricing_source, pricing_version)``
    """
    tracker = _active_tracker
    if tracker is None:
        return (Decimal("0"), "unknown", "unknown", None)

    # 1. Rate registry: explicit MCP tool rate. The registry's unit is part of
    # the contract: a per-credit/page rate must never silently become a
    # per-call rate merely because the invocation happened through MCP.
    mcp_key = f"mcp:{tool_name}"
    entry = tracker.rate_registry.get(mcp_key)
    if entry is not None:
        quantity = _rate_quantity(entry.per, quantities)
        if quantity is None:
            return (Decimal("0"), "unknown", "unknown", None)
        return (
            entry.cost_usd * quantity,
            "computed",
            "rate_registry",
            tracker.rate_registry.pricing_version,
        )

    # 2. Reviewed alias from the active signed service artifact. This still
    # resolves only an explicitly registered user rate; server list prices are
    # not copied into the tracker rate registry. As above, the unit must have
    # observable evidence before money is computed.
    catalog_key = _service_rate_key(tracker, tool_name)
    if catalog_key is not None:
        entry = tracker.rate_registry.get(catalog_key)
        if entry is not None:
            quantity = _rate_quantity(entry.per, quantities)
            if quantity is None:
                return (Decimal("0"), "unknown", "unknown", None)
            return (
                entry.cost_usd * quantity,
                "computed",
                "rate_registry",
                tracker.rate_registry.pricing_version,
            )

    # 3. Unknown tool or no rate registered
    return (Decimal("0"), "unknown", "unknown", None)


def _rate_quantity(per: str, quantities: Mapping[str, Decimal]) -> Decimal | None:
    unit = per.strip().lower().replace("-", "_").replace(" ", "_")
    if unit in _CALL_RATE_UNITS:
        return Decimal(1)
    if unit in _CREDIT_RATE_UNITS:
        return quantities.get("credit_count")
    return None


def _service_rate_key(tracker: Any, tool_name: str) -> str | None:
    """Resolve an MCP alias only from the active signed service artifact."""
    catalog = getattr(tracker, "_service_catalog", None)
    lookup = getattr(catalog, "lookup_mcp_tool", None)
    if callable(lookup):
        try:
            entry = lookup(tool_name)
        except Exception:  # pragma: no cover - a validated catalog should not raise
            _log.debug("dexcost: signed MCP alias lookup failed", exc_info=True)
        else:
            key = getattr(entry, "key", None)
            if isinstance(key, str) and key:
                return key
    return None


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    try:
        return getattr(value, key, None)
    except Exception:  # pragma: no cover - defensive; provider properties can raise
        return None


def _nested_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        current = _mapping_value(current, part)
        if current is None:
            return None
    return current


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not quantity.is_finite() or quantity < 0:
        return None
    return quantity


def _json_content_payloads(result: Any) -> list[Mapping[str, Any]]:
    """Return bounded object-shaped JSON tool results without retaining content."""
    payloads: list[Mapping[str, Any]] = []
    structured = _mapping_value(result, "structuredContent")
    if structured is None:
        structured = _mapping_value(result, "structured_content")
    if isinstance(structured, Mapping):
        payloads.append(structured)

    content = _mapping_value(result, "content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return payloads
    for block in content:
        block_type = _mapping_value(block, "type")
        text = _mapping_value(block, "text")
        if block_type != "text" or not isinstance(text, str):
            continue
        if len(text.encode("utf-8", errors="ignore")) > _BILLING_JSON_LIMIT_BYTES:
            continue
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, Mapping):
            payloads.append(decoded)
    return payloads


def _mcp_quantities(tool_name: str, result: Any | None) -> dict[str, Decimal]:
    """Extract only provider-native, quantity-only billing evidence.

    MCP itself defines no standard billing field. We therefore accept explicit
    credit totals only for official Firecrawl/Tavily tool namespaces whose
    provider contracts document credit metering. Result content is inspected
    in memory and never copied into telemetry.
    """
    if result is None or not tool_name.startswith(("firecrawl_", "tavily_")):
        return {}

    payloads = _json_content_payloads(result)
    meta = _mapping_value(result, "meta")
    if meta is None:
        meta = _mapping_value(result, "_meta")
    if isinstance(meta, Mapping):
        payloads.insert(0, meta)

    candidates: set[Decimal] = set()
    for payload in payloads:
        for path in (
            "creditsUsed",
            "credits_used",
            "usage.credits",
            "usage.creditsUsed",
            "usage.credits_used",
        ):
            quantity = _nonnegative_decimal(_nested_value(payload, path))
            if quantity is not None:
                candidates.add(quantity)

    # Conflicting provider quantities are not resolved heuristically.
    if len(candidates) != 1:
        return {}
    return {"credit_count": next(iter(candidates))}


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


def _result_is_error(result: Any) -> bool:
    """Return whether an MCP tool result signals a protocol-level error."""
    for attribute in ("isError", "is_error"):
        try:
            value = getattr(result, attribute, None)
        except Exception:  # pragma: no cover - defensive; properties can raise
            continue
        if value:
            return True
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return False


def _record_tool_call(
    *,
    tool_name: str,
    instance: Any,
    latency_ms: int,
    is_error: bool,
    error_type: str | None = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    result: Any | None = None,
) -> Event | None:
    """Create and persist an external_cost event for an MCP tool call.

    The tool itself is the resource being charged for, so the event carries a
    ``resource = {"type": "tool", "id": <tool name>}`` identity. The provider
    name/service stay derived from ``service_name`` exactly as before
    (``mcp`` / ``<tool name>``).
    """
    tracker = _active_tracker
    if tracker is None:
        return None
    task = task if task is not None else get_current_task()
    if task is None:
        return None

    quantities = _mcp_quantities(tool_name, result)
    cost_usd, cost_confidence, pricing_source, pricing_version = _resolve_cost(
        tool_name, quantities
    )

    # Best-effort extraction of MCP server info
    raw_server = getattr(instance, "_server_name", None)
    mcp_server = str(raw_server).strip()[:256] if raw_server else "unknown"
    operation_status = "failed" if is_error else "succeeded"

    details: dict[str, Any] = {
        "mcp_tool": tool_name,
        "mcp_server": mcp_server,
        "latency_ms": latency_ms,
        "is_error": is_error,
        "attribution_component": "external",
        "attribution_operation_name": "mcp.call_tool",
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "tool",
        "attribution_resource_id": tool_name,
        "attribution_usage_lines": [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"},
            *(
                [
                    {
                        "metric": "credit_count",
                        "quantity": str(quantities["credit_count"]),
                        "unit": "Credits",
                    }
                ]
                if quantities.get("credit_count", Decimal(0)) > 0
                else []
            ),
        ],
        "provider_usage_privacy": "quantities_only",
    }
    if is_error:
        details["error_type"] = error_type or TOOL_ERROR_TYPE

    event = Event(
        task_id=task.task_id,
        event_type="external_cost",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name=f"mcp:{tool_name}",
        provider="mcp",
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
