"""dexcost — Agent Unit Economics SDK.

Track end-to-end business-task costs for AI agents, including LLM calls,
non-LLM service fees, and retry waste, attributed to customers, projects,
and workflows.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from collections.abc import Generator
from decimal import Decimal
from typing import Any

__version__ = "0.1.0"

from dexcost.clients import TrackedAnthropic, TrackedOpenAI
from dexcost.config import DexcostConfig, InvalidAPIKeyError, validate_api_key
from dexcost.context import (
    DexcostContext,
    async_task_context,
    clear_context,
    get_context,
    get_current_task,
    set_context as _set_context_impl,
    set_current_task,
    task_context,
)
from dexcost.instruments import (
    instrument_anthropic,
    instrument_bedrock,
    instrument_cohere,
    instrument_gemini,
    instrument_litellm,
    instrument_mcp,
    instrument_openai,
    uninstrument_anthropic,
    uninstrument_bedrock,
    uninstrument_cohere,
    uninstrument_gemini,
    uninstrument_litellm,
    uninstrument_mcp,
    uninstrument_openai,
)
from dexcost.models import (
    CostConfidence,
    Event,
    EventType,
    PricingSource,
    Task,
    TaskStatus,
)
from dexcost.pricing import CostResult, PricingEngine
from dexcost.rates import RateEntry, RateRegistry
from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict
from dexcost.schema import validate
from dexcost.sync import SyncWorker
from dexcost.service_catalog import ServiceCatalog
from dexcost.session import SessionManager, get_session_manager
from dexcost.tracker import ALL_SUPPORTED_INSTRUMENTS, CostTracker, TrackedTask

_global_config: DexcostConfig | None = None
_sync_worker: SyncWorker | None = None
_pricing_engine: PricingEngine | None = None
_global_tracker: CostTracker | None = None


def _atexit_handler() -> None:
    """Flush pending events and close connections on process exit."""
    global _sync_worker, _global_tracker
    if _sync_worker is not None:
        try:
            _sync_worker.flush()
        except Exception:
            pass
        try:
            _sync_worker.stop()
        except Exception:
            pass
    _sync_worker = None
    _global_tracker = None


def set_context(
    customer_id: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    agent: str | None = None,
) -> None:
    """Set the attribution context for subsequent LLM calls and tasks.

    Args:
        customer_id: Identifier for the customer.
        project_id: Identifier for the project.
        metadata: Optional dict of extra metadata.
        agent: Optional agent name — used as task_type for auto-created
            session tasks instead of the default ``"agent_session"``.
    """
    _set_context_impl(
        customer_id=customer_id,
        project_id=project_id,
        metadata=metadata,
        agent=agent,
    )


def init(
    api_key: str | None = None,
    storage: str | None = None,
    buffer_path: str | None = None,
    batch_size: int = 100,
    flush_interval: float = 5.0,
    auto_instrument: list[str] | None = None,
    redact_fields: list[str] | None = None,
    hash_customer_id: bool = False,
    track_http: bool = True,
    service_catalog_url: str | None = None,
    environment: str | None = None,
    enable_retry_heuristics: bool = False,
    retry_heuristic_window: float | None = None,
    retry_heuristic_threshold: float | None = None,
    track_network: bool = True,
    network_event_threshold_bytes: int = 102_400,
    network_event_on_error: bool = True,
    network_event_latency_ms: int = 0,
) -> DexcostConfig:
    """Initialize dexcost SDK configuration (US-017).

    When a valid API key is provided (or set via ``DEXCOST_API_KEY``),
    a background :class:`SyncWorker` is started to push buffered events
    to the Control Layer (US-016).

    Args:
        enable_retry_heuristics: Opt in to the advanced
            :class:`~dexcost.heuristics.RetryHeuristicEngine` (US-036).
            Off by default.
        retry_heuristic_window: Optional sliding-window size in seconds for
            heuristic retry detection. Defaults to the tracker's retry window.
        retry_heuristic_threshold: Optional confidence threshold (0.0–1.0)
            for flagging an event as a heuristic retry.
        track_network: Enable or disable network/egress byte capture. Default ``True``.
        network_event_threshold_bytes: Combined request+response bytes above which
            an un-cataloged HTTP call emits a ``network`` event. Default 100 KiB
            (102 400 bytes).
        network_event_on_error: Emit a ``network`` event for un-cataloged HTTP calls
            whose response status is >= 400. Default ``True``.
        network_event_latency_ms: Emit a ``network`` event when call latency exceeds
            this many milliseconds. ``0`` disables latency-based emission (default).
    """
    global _global_config, _sync_worker, _global_tracker
    _global_config = DexcostConfig(
        api_key=api_key,
        storage=storage,
        buffer_path=buffer_path,
        batch_size=batch_size,
        flush_interval_seconds=flush_interval,
        redact_fields=redact_fields or [],
        hash_customer_id=hash_customer_id,
        environment=environment,
        track_network=track_network,
        network_event_threshold_bytes=network_event_threshold_bytes,
        network_event_on_error=network_event_on_error,
        network_event_latency_ms=network_event_latency_ms,
    )

    # v2 network-cost — kick off non-blocking cloud detection.  No-op when
    # track_network is off.  Phase 1a/1b run synchronously here (sub-ms);
    # Phase 2 runs on a daemon thread that never blocks init().
    from dexcost.cloud_detect import start_background_detection as _start_detect
    _start_detect(track_network=_global_config.track_network)

    # Dev mode — console output, no cloud push
    if _global_config.is_dev:
        from dexcost.dev_console import enable_dev_mode
        enable_dev_mode()

    # Patch ThreadPoolExecutor to propagate contextvars to child threads.
    # Libraries like LangExtract, OpenAI, etc. use ThreadPoolExecutor for
    # parallel work — without this, child threads can't find the active task.
    from dexcost.context import patch_thread_context
    patch_thread_context()

    # Create the global tracker with auto-instrumentation.
    # Thread retry-heuristic settings through so the advanced
    # RetryHeuristicEngine (US-036) is reachable via init().
    _global_tracker = CostTracker(
        auto_instrument=auto_instrument,
        enable_retry_heuristics=enable_retry_heuristics,
        retry_heuristic_window=retry_heuristic_window,
        retry_heuristic_threshold=retry_heuristic_threshold,
    )

    # Wire the browser adapter to the tracker's storage so track_browser()
    # cost events are persisted durably and shipped by the SyncWorker. The
    # browser adapter has no init flag — it is opt-in via its context manager —
    # so storage is wired unconditionally and used only if track_browser runs.
    from dexcost.adapters.browser import set_storage as _set_browser_storage

    _set_browser_storage(_global_tracker._storage)

    # Start background sync worker in cloud mode (US-016)
    if _global_config.storage_mode == "cloud" and not _global_config.is_dev:
        from dexcost.storage.sqlite import SQLiteStorage

        sync_storage = SQLiteStorage(db_path=_global_config.buffer_path)
        _sync_worker = SyncWorker(
            config=_global_config,
            storage=sync_storage,
            db_path=_global_config.buffer_path,
        )
        _sync_worker.start()
        atexit.register(_atexit_handler)

    # Non-blocking pricing data refresh from Control Layer (US-044)
    if _global_config.storage_mode == "cloud" and not _global_config.is_dev:
        try:
            _pricing_engine = PricingEngine(api_key=_global_config.api_key)
            _pricing_engine.start_background_refresh(_global_config.endpoint)
        except Exception:
            pass  # Fail-silent — bundled pricing is always available

    # Auto-track HTTP calls via service catalog
    if track_http:
        from dexcost.adapters.http import (
            get_catalog,
            set_network_config as _set_network_config,
            set_storage as _set_http_storage,
            track_http as _track_http_fn,
        )

        _track_http_fn()
        # Wire the adapter to the tracker's storage so HTTP cost events are
        # persisted durably and shipped by the SyncWorker — without this they
        # would only land in the adapter's in-memory list and never sync.
        _set_http_storage(_global_tracker._storage)
        # Wire the SDK config so the adapter uses the caller's network-capture
        # settings (thresholds, on/off toggles) rather than hard-coded defaults.
        _set_network_config(_global_config)
        if service_catalog_url:
            catalog = get_catalog()
            catalog.refresh_from_url(service_catalog_url)

    return _global_config


@contextmanager
def task(
    task_type: str = "",
    metadata: dict[str, Any] | None = None,
) -> Generator[TrackedTask, None, None]:
    """Group multiple costs into one business task.

    Reads ``customer_id`` and ``project_id`` from :func:`set_context` if set.

    Args:
        task_type: Identifier for the kind of task (e.g. ``"resolve_ticket"``).
        metadata: Optional dict of extra metadata.

    Yields:
        A :class:`TrackedTask` handle.

    Raises:
        RuntimeError: If ``dexcost.init()`` has not been called.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    ctx = get_context()
    with _global_tracker.task(
        task_type=task_type,
        customer_id=ctx.customer_id if ctx else None,
        project_id=ctx.project_id if ctx else None,
        metadata=metadata,
    ) as t:
        yield t


def record_cost(
    service: str,
    cost_usd: Decimal | str,
    *,
    event_type: str = "external_cost",
    cost_confidence: str = "exact",
    pricing_source: str = "manual",
    pricing_version: str | None = None,
    details: dict[str, Any] | None = None,
) -> Event:
    """Record a non-LLM cost event against the current active task.

    Args:
        service: Name of the external service (e.g. ``"google_maps_api"``).
        cost_usd: Cost in USD (Decimal or string).
        event_type: ``"external_cost"`` (default) or ``"compute_cost"``.
        cost_confidence: One of ``exact``, ``computed``, ``estimated``, ``unknown``.
        pricing_source: Source of pricing data (default ``"manual"``).
        pricing_version: Optional hash referencing the rate snapshot used.
        details: Optional dict of extra metadata.

    Returns:
        The persisted :class:`Event`.

    Raises:
        RuntimeError: If ``dexcost.init()`` has not been called or no active task exists.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    current = get_current_task()
    if current is None:
        raise RuntimeError("No active task — use dexcost.task() context manager first")
    tracked = TrackedTask(current, _global_tracker._storage, _global_tracker)
    return tracked.record_cost(
        service=service,
        cost_usd=cost_usd,
        event_type=event_type,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        details=details,
    )


def close() -> None:
    """Shut down the global tracker and flush any pending events.

    Safe to call even if ``init()`` has not been called (no-op).
    """
    global _global_tracker, _sync_worker
    if _sync_worker is not None:
        _sync_worker.flush()
        _sync_worker.stop()
        _sync_worker = None
    _global_tracker = None


def flush() -> None:
    """Force immediate sync of buffered events to the Control Layer.

    No-op if the SDK is in local-only mode or ``init()`` has not been called.
    """
    if _sync_worker is not None:
        _sync_worker.flush()


__all__ = [
    "ALL_SUPPORTED_INSTRUMENTS",
    "CostConfidence",
    "CostResult",
    "CostTracker",
    "DexcostConfig",
    "DexcostContext",
    "Event",
    "EventType",
    "InvalidAPIKeyError",
    "PricingEngine",
    "PricingSource",
    "RateEntry",
    "RateRegistry",
    "ServiceCatalog",
    "SessionManager",
    "SyncWorker",
    "Task",
    "TaskStatus",
    "TrackedAnthropic",
    "TrackedOpenAI",
    "TrackedTask",
    "__version__",
    "async_task_context",
    "clear_context",
    "close",
    "enforce_metadata_limit",
    "flush",
    "get_context",
    "get_current_task",
    "hash_value",
    "init",
    "instrument_anthropic",
    "instrument_bedrock",
    "instrument_cohere",
    "instrument_gemini",
    "instrument_litellm",
    "instrument_mcp",
    "instrument_openai",
    "record_cost",
    "redact_dict",
    "set_context",
    "set_current_task",
    "task",
    "task_context",
    "uninstrument_anthropic",
    "uninstrument_bedrock",
    "uninstrument_cohere",
    "uninstrument_gemini",
    "uninstrument_litellm",
    "uninstrument_mcp",
    "uninstrument_openai",
    "validate",
    "validate_api_key",
]
