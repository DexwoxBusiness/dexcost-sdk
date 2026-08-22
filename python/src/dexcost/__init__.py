"""dexcost — Agent Unit Economics SDK.

Track end-to-end business-task costs for AI agents, including LLM calls,
non-LLM service fees, and retry waste, attributed to customers, projects,
and workflows.
"""

# This module is the package's deliberate public re-export surface.
# ruff: noqa: F401

from __future__ import annotations

import atexit
import contextvars
import logging
import os
import threading
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from dexcost.attribution import (
    ATTRIBUTION_COMPONENTS,
    ATTRIBUTION_UNIT_BY_METRIC,
    ATTRIBUTION_USAGE_METRICS,
    ATTRIBUTION_USAGE_UNITS,
    ATTRIBUTION_V2_CONTRACT_VERSION,
    ATTRIBUTION_V3_CONTRACT_VERSION,
    AttributionAttemptIdentityV3,
    AttributionBillingDimension,
    AttributionBillingDimensionValue,
    AttributionCapabilityIdentityV3,
    AttributionCapabilityInvocationV3,
    AttributionCapabilityKindV3,
    AttributionCapabilitySourceV3,
    AttributionComponent,
    AttributionConfidence,
    AttributionCostEvidenceSource,
    AttributionCostEvidenceV2,
    AttributionCostEvidenceV3,
    AttributionEventV2,
    AttributionEventV3,
    AttributionLifecycleState,
    AttributionLifecycleV2,
    AttributionLifecycleV3,
    AttributionObservationV3,
    AttributionOperationErrorV3,
    AttributionOperationIdentityV3,
    AttributionOperationStatusV3,
    AttributionProviderIdentityV2,
    AttributionProviderIdentityV3,
    AttributionResourceV2,
    AttributionResourceV3,
    AttributionTaskIngestV1,
    AttributionUsageLineV2,
    AttributionUsageLineV3,
    AttributionUsageMetric,
    AttributionUsageMetricV3,
    AttributionUsagePeriodV2,
    AttributionUsagePeriodV3,
    AttributionUsageUnit,
    AttributionUsageUnitV3,
    AttributionV2ValidationIssue,
    AttributionV2ValidationResult,
    AttributionV3ValidationIssue,
    AttributionV3ValidationResult,
    assert_attribution_event_v2,
    assert_attribution_observation_v3,
    to_attribution_event_v2,
    to_attribution_event_v3,
    to_attribution_observation_v3,
    to_attribution_task_ingest_v1,
    to_business_identity_revision_v1,
    validate_attribution_event_v2,
    validate_attribution_observation_v3,
)
from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import (
    capability_context,
    get_capability,
    set_capability,
)
from dexcost.catalog_runtime import CatalogRuntime, CatalogRuntimeStatus
from dexcost.clients import TrackedAnthropic, TrackedOpenAI
from dexcost.compute_wrap import (
    wrap_azure_functions_handler,
    wrap_cloud_functions_handler,
    wrap_cloud_run_handler,
    wrap_lambda_handler,
    wrap_vercel_handler,
)
from dexcost.config import (
    DexcostConfig,
    InvalidAPIKeyError,
    resolve_catalog_trust_policy,
    validate_api_key,
)
from dexcost.context import (
    DexcostContext,
    _reset_current_task,
    async_task_context,
    clear_context,
    get_context,
    get_current_task,
    set_current_task,
    task_context,
)
from dexcost.context import (
    set_context as _set_context_impl,
)
from dexcost.delivery import (
    DeliveryErrorCallback,
    DeliveryErrorEvent,
    DeliveryStatus,
    local_delivery_status,
    on_delivery_error,
    remove_delivery_error_callback,
)
from dexcost.gpu_wrap import (
    wrap_modal_handler,
    wrap_replicate_handler,
    wrap_runpod_handler,
)
from dexcost.idempotency import (
    get_idempotency_key,
    idempotency_key,
    set_idempotency_key,
)
from dexcost.instruments import (
    instrument_anthropic,
    instrument_bedrock,
    instrument_cohere,
    instrument_fal,
    instrument_gemini,
    instrument_litellm,
    instrument_mcp,
    instrument_ollama,
    instrument_openai,
    instrument_openrouter,
    instrument_perplexity,
    uninstrument_anthropic,
    uninstrument_bedrock,
    uninstrument_cohere,
    uninstrument_fal,
    uninstrument_gemini,
    uninstrument_litellm,
    uninstrument_mcp,
    uninstrument_ollama,
    uninstrument_openai,
    uninstrument_openrouter,
    uninstrument_perplexity,
)
from dexcost.integrations import track_crewai, track_griptape
from dexcost.models import (
    CostConfidence,
    Event,
    EventType,
    PricingSource,
    Task,
    TaskStatus,
)
from dexcost.models.capability import (
    CapabilityIdentity,
    CapabilityInvocation,
    CapabilityKind,
    CapabilitySource,
)
from dexcost.models.outcome import (
    OutcomeInput,
    OutcomeRevision,
    OutcomeState,
    OutcomeValue,
    OutcomeValueType,
)
from dexcost.models.pricing_explanation import (
    PricingExplanation,
    PricingExplanationStatus,
    PricingProvenance,
)
from dexcost.models.revenue import (
    RevenueAmount,
    RevenueInput,
    RevenueRevision,
    RevenueSource,
    RevenueSourceType,
    RevenueState,
)
from dexcost.models.tool import ToolQuantityInput, ToolUsage
from dexcost.pricing import CostResult, PricingEngine
from dexcost.rates import InfrastructureRateEntry, RateEntry, RateRegistry
from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict
from dexcost.schema import SchemaNotFoundError, validate
from dexcost.service_catalog import ServiceCatalog
from dexcost.session import SessionManager, get_session_manager
from dexcost.sync import SyncWorker
from dexcost.tool_tracking import decorate_tool
from dexcost.tracker import (
    ALL_SUPPORTED_INSTRUMENTS,
    AttachedTask,
    CostTracker,
    ToolDimensionInput,
    ToolOperationStatus,
    TrackedTask,
)
from dexcost.webhooks import (
    WebhookHeader,
    WebhookSecret,
    WebhookVerificationError,
    assert_webhook_signature,
    verify_webhook_signature,
)

__version__ = "0.19.0"
_log = logging.getLogger(__name__)

_global_config: DexcostConfig | None = None
_sync_worker: SyncWorker | None = None
_pricing_engine: PricingEngine | None = None
_global_tracker: CostTracker | None = None
_catalog_runtime: CatalogRuntime | None = None
_service_catalog_refresh_url: str | None = None
_service_catalog_refresh_api_key: str | None = None
# Sprint 1 Theme B / §2.2.4: register_at_fork must be installed exactly
# once per process (not once per init() call) — guards against the hook
# being registered multiple times if init() is called after close().
_fork_hook_registered: bool = False


def _same_origin(left: str, right: str) -> bool:
    try:
        left_url = urlparse(left)
        right_url = urlparse(right)
        left_port = left_url.port or (443 if left_url.scheme == "https" else 80)
        right_port = right_url.port or (443 if right_url.scheme == "https" else 80)
        return (
            left_url.scheme,
            left_url.hostname,
            left_port,
        ) == (
            right_url.scheme,
            right_url.hostname,
            right_port,
        )
    except ValueError:
        return False


def _start_service_catalog_refresh() -> None:
    if _service_catalog_refresh_url is None:
        return
    try:
        from dexcost.adapters.http import get_catalog

        catalog = get_catalog()
        threading.Thread(
            target=catalog.refresh_from_url,
            args=(_service_catalog_refresh_url, _service_catalog_refresh_api_key),
            name="dexcost-service-catalog-refresh",
            daemon=True,
        ).start()
    except Exception:
        pass


def _reinit_after_fork() -> None:
    """Re-establish per-process state in a child after os.fork().

    The child inherits the parent's SQLite connection fd (corrupting if
    both processes write) and the parent's SyncWorker Thread object
    (which is a dangling reference — the underlying OS thread is not
    copied). This handler is registered via os.register_at_fork in
    init() exactly once per process. Sprint 1 Theme B / §2.2.4.
    """
    global _sync_worker, _pricing_engine, _global_tracker, _global_config
    global _catalog_runtime

    # Drop the inherited SyncWorker reference WITHOUT calling .stop() —
    # the underlying thread no longer exists in the child, so the
    # threading.Event / join() in stop() would deadlock.
    _sync_worker = None

    # Close the inherited SQLite connection on the tracker and recreate
    # storage so the child gets a fresh fd. Without this, parent + child
    # writes interleave through the same fd and corrupt the file.
    if _global_tracker is not None and _global_config is not None:
        with suppress(Exception):
            _global_tracker._storage.close()
        from dexcost.storage.sqlite import SQLiteStorage
        _global_tracker._storage = SQLiteStorage(db_path=_global_config.buffer_path)

        # Re-wire any adapter modules that hold their own reference.
        from dexcost.adapters.browser import set_storage as _set_browser_storage
        _set_browser_storage(_global_tracker._storage)

        # Restart the sync worker on a fresh thread + fresh connection if
        # we're in cloud mode. The child gets its own background pusher.
        if _global_config.storage_mode == "cloud" and not _global_config.is_dev:
            sync_storage = SQLiteStorage(db_path=_global_config.buffer_path)
            _sync_worker = SyncWorker(
                config=_global_config,
                storage=sync_storage,
                db_path=_global_config.buffer_path,
            )
            _sync_worker.start()

            with suppress(Exception):
                _pricing_engine = _global_tracker.pricing
                _pricing_engine.set_api_key(_global_config.api_key)
                previous_catalog_runtime = _catalog_runtime
                track_http = (
                    True
                    if previous_catalog_runtime is None
                    else previous_catalog_runtime._track_http
                )
                _catalog_runtime = CatalogRuntime(
                    endpoint=_global_config.endpoint,
                    db_path=_global_config.buffer_path,
                    tracker=_global_tracker,
                    track_http=track_http,
                    api_key=_global_config.api_key,
                    trusted_keys=(
                        None
                        if previous_catalog_runtime is None
                        else previous_catalog_runtime._trusted_keys
                    ),
                    require_signature=(
                        False
                        if previous_catalog_runtime is None
                        else previous_catalog_runtime._require_signature
                    ),
                )
                _catalog_runtime.load_cached()
                _catalog_runtime.start()

            _start_service_catalog_refresh()


def _atexit_handler() -> None:
    """Flush pending events and close connections on process exit."""
    global _sync_worker, _global_tracker, _pricing_engine, _catalog_runtime
    global _service_catalog_refresh_url, _service_catalog_refresh_api_key
    if _sync_worker is not None:
        with suppress(Exception):
            _sync_worker.flush()
        with suppress(Exception):
            _sync_worker.stop()
    if _catalog_runtime is not None:
        with suppress(Exception):
            _catalog_runtime.close()
    if _pricing_engine is not None:
        _pricing_engine.close()
    _sync_worker = None
    _global_tracker = None
    _pricing_engine = None
    _catalog_runtime = None
    _service_catalog_refresh_url = None
    _service_catalog_refresh_api_key = None


def set_context(
    customer_id: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    agent: str | None = None,
    agent_version: str | None = None,
    workflow_id: str | None = None,
    workflow_session_id: str | None = None,
    *,
    user_id: str | None = None,
    product_id: str | None = None,
) -> None:
    """Set the attribution context for subsequent LLM calls and tasks.

    Args:
        customer_id: Identifier for the customer.
        project_id: Identifier for the project.
        metadata: Optional dict of extra metadata.
        user_id: Identifier for the end user the work is performed for.
        product_id: Identifier for the product surface driving the work.
        agent: Stable agent identifier, kept separate from task type.
        agent_version: Version or deployment identifier for ``agent``.
        workflow_id: Stable workflow identifier.
        workflow_session_id: Optional workflow execution/session identifier.
    """
    _set_context_impl(
        customer_id=customer_id,
        project_id=project_id,
        metadata=metadata,
        user_id=user_id,
        product_id=product_id,
        agent=agent,
        agent_version=agent_version,
        workflow_id=workflow_id,
        workflow_session_id=workflow_session_id,
    )


def init(
    api_key: str | None = None,
    storage: str | None = None,
    endpoint: str | None = None,
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
    compute_billing_overrides: dict[str, str] | None = None,
    k8s_node_aware: bool = False,
    rates_path: str | os.PathLike[str] | None = None,
    catalog_refresh_interval: float = 86_400,
    catalog_refresh_jitter: float = 0.1,
    catalog_trusted_keys: Mapping[str, str | bytes] | None = None,
    catalog_require_signature: bool | None = None,
) -> DexcostConfig:
    """Initialize dexcost SDK configuration (US-017).

    When a valid API key is provided (or set via ``DEXCOST_API_KEY``),
    a background :class:`SyncWorker` is started to push buffered events
    to the Control Layer (US-016).

    Args:
        endpoint: Explicit Control Layer URL (e.g. ``http://localhost:3000``
            for e2e). Defaults to the production endpoint
            ``https://api.dexcost.io``. Must start with ``http://`` or
            ``https://``; otherwise the production default is used. The
            endpoint is NOT read from the environment — this is the only
            way to override it.
        enable_retry_heuristics: Opt in to the advanced
            :class:`~dexcost.heuristics.RetryHeuristicEngine` (US-036).
            Off by default.
        retry_heuristic_window: Optional sliding-window size in seconds for
            heuristic retry detection. Defaults to the tracker's retry window.
        retry_heuristic_threshold: Optional confidence threshold (0.0-1.0)
            for flagging an event as a heuristic retry.
        track_network: Enable or disable network/egress byte capture. Default ``True``.
        network_event_threshold_bytes: Combined request+response bytes above which
            an un-cataloged HTTP call emits a ``network`` event. Default 100 KiB
            (102 400 bytes).
        network_event_on_error: Emit a ``network`` event for un-cataloged HTTP calls
            whose response status is >= 400. Default ``True``.
        network_event_latency_ms: Emit a ``network`` event when call latency exceeds
            this many milliseconds. ``0`` disables latency-based emission (default).
        rates_path: Optional explicit path to a versioned ``rates.yaml`` file.
            User-owned GPU and network rates are loaded before capture starts.
        catalog_refresh_interval: Seconds between server catalog checks. Network
            refresh always runs in the background and never blocks provider calls.
        catalog_refresh_jitter: Fractional random jitter applied to the refresh
            interval to avoid synchronized client traffic. Must be 0 through 0.5.
        catalog_trusted_keys: Ed25519 public keys keyed by manifest ``key_id``.
            Values are raw 32-byte keys or unpadded base64url. Rotated keys may
            overlap. Signed manifests are verified whenever this mapping is set.
        catalog_require_signature: Reject unsigned manifests and unsigned durable
            cache entries. When omitted, this defaults to ``True`` if trusted
            keys are configured and ``False`` otherwise. Set ``False`` explicitly
            only for a controlled unsigned-to-signed migration.
    """
    global _global_config, _sync_worker, _global_tracker, _pricing_engine
    global _catalog_runtime
    global _fork_hook_registered
    global _service_catalog_refresh_url, _service_catalog_refresh_api_key

    # Sprint 1 Theme B / §2.2.4(a): idempotency guard. A second init()
    # call without an intervening close() would otherwise orphan the
    # existing SyncWorker thread (the previous reference is dropped
    # without .stop()) — duplicate workers then race on the same SQLite
    # file. Log + return the existing tracker.
    if _global_tracker is not None:
        _log.warning(
            "dexcost.init() called more than once without an intervening "
            "close(); ignoring this call and keeping the existing tracker. "
            "If you intend to reconfigure, call dexcost.close() first."
        )
        return _global_config  # type: ignore[return-value]

    # Resolve security configuration before mutating globals, opening storage,
    # or starting background work. Malformed or incomplete trust settings are
    # operator errors and must stop startup instead of silently disabling
    # signature enforcement.
    resolved_catalog_keys, resolved_catalog_signature_requirement = (
        resolve_catalog_trust_policy(
            catalog_trusted_keys,
            catalog_require_signature,
        )
    )

    # Refresh configuration belongs to one accepted init lifecycle. Clear it
    # before applying the new config so track_http=False cannot inherit a URL
    # or API key from a previous initialization.
    _service_catalog_refresh_url = None
    _service_catalog_refresh_api_key = None

    _global_config = DexcostConfig(
        api_key=api_key,
        storage=storage,
        endpoint_override=endpoint,
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
    # The tracker and sync worker must use the same configured durable buffer.
    # Otherwise capture writes to ~/.dexcost/buffer.db while the worker polls
    # buffer_path, silently stranding events.
    from dexcost.storage.sqlite import SQLiteStorage

    tracker_storage = SQLiteStorage(db_path=_global_config.buffer_path)
    candidate_tracker = CostTracker(
        storage=tracker_storage,
        auto_instrument=auto_instrument,
        enable_retry_heuristics=enable_retry_heuristics,
        retry_heuristic_window=retry_heuristic_window,
        retry_heuristic_threshold=retry_heuristic_threshold,
        compute_billing_overrides=compute_billing_overrides,
        k8s_node_aware=k8s_node_aware,
    )
    try:
        if rates_path is not None:
            candidate_tracker.load_rates(Path(rates_path))
    except Exception:
        candidate_tracker.storage.close()
        raise
    _global_tracker = candidate_tracker
    _pricing_engine = _global_tracker.pricing

    # Load a complete durable release before considering bundled bootstrap
    # catalogs authoritative. Network refresh starts separately below and can
    # never delay initialization or a provider call.
    try:
        _catalog_runtime = CatalogRuntime(
            endpoint=_global_config.endpoint,
            db_path=_global_config.buffer_path,
            tracker=_global_tracker,
            track_http=track_http,
            api_key=_global_config.api_key,
            refresh_interval_seconds=catalog_refresh_interval,
            refresh_jitter_ratio=catalog_refresh_jitter,
            trusted_keys=resolved_catalog_keys,
            require_signature=resolved_catalog_signature_requirement,
        )
        _catalog_runtime.load_cached()
    except Exception:
        _catalog_runtime = None
        _log.warning(
            "dexcost catalog cache unavailable; using bundled bootstrap data",
            exc_info=True,
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

        # Sprint 1 Theme B / §2.2.4(b): fork safety. After os.fork() the
        # child inherits the parent's SQLite connection fd and the
        # SyncWorker Thread object (the thread itself does not survive
        # fork — only the Python wrapper is copied). Concurrent writes
        # from two processes to the same fd corrupt SQLite; the dangling
        # thread object would make stop() / flush() hang. Reset both in
        # the child by closing inherited resources and re-running the
        # sync-worker bootstrap. Registered exactly once per process.
        if not _fork_hook_registered and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_reinit_after_fork)
            _fork_hook_registered = True

    # Non-blocking, atomic refresh of every catalog family from the Control Layer.
    if (
        _global_config.storage_mode == "cloud"
        and not _global_config.is_dev
        and _catalog_runtime is not None
    ):
        with suppress(Exception):
            _catalog_runtime.start()

    # Auto-track HTTP calls via service catalog
    if track_http:
        from dexcost.adapters.http import (
            set_network_config as _set_network_config,
        )
        from dexcost.adapters.http import (
            set_storage as _set_http_storage,
        )
        from dexcost.adapters.http import (
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
        # Explicit legacy URLs remain available for compatibility. The default
        # path is the atomic release runtime above, never an independent service
        # catalog update that could drift from the other pricing families.
        catalog_url = service_catalog_url
        catalog_api_key = None
        if (
            catalog_url is not None
            and _global_config.api_key
            and _same_origin(catalog_url, _global_config.endpoint)
        ):
            catalog_api_key = _global_config.api_key
        _service_catalog_refresh_url = catalog_url
        _service_catalog_refresh_api_key = catalog_api_key
        _start_service_catalog_refresh()

    return _global_config


def attach_task(
    task_id: uuid.UUID | str,
    *,
    task_type: str = "attached",
    root_task_id: uuid.UUID | str | None = None,
    parent_task_id: uuid.UUID | str | None = None,
) -> AttachedTask:
    """Attach a non-owning handle to a canonical task from another process.

    The returned handle can record costs, tools, outcomes, and revenue. Use it
    as ``with dexcost.attach_task(task_id):`` to propagate that task identity
    into automatic provider instrumentation. It never ends or rewrites the
    remote task itself.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    return _global_tracker.attach_task(
        task_id,
        task_type=task_type,
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
    )


@contextmanager
def task(
    task_type: str = "",
    metadata: dict[str, Any] | None = None,
    *,
    experiment_id: str | None = None,
    variant: str | None = None,
    task_id: uuid.UUID | str | None = None,
    root_task_id: uuid.UUID | str | None = None,
    parent_task_id: uuid.UUID | str | None = None,
    track_gpu: bool = False,
) -> Generator[TrackedTask, None, None]:
    """Group multiple costs into one business task.

    Reads customer, project, user, product, agent, and workflow identity
    from :func:`set_context` if set.

    Args:
        task_type: Identifier for the kind of task (e.g. ``"resolve_ticket"``).
        metadata: Optional dict of extra metadata.
        experiment_id: Optional experiment grouping.
        variant: Optional variant label within the experiment.
        task_id: Optional caller-owned UUID for cross-process correlation.
        root_task_id: Optional canonical campaign/workflow root UUID.
        parent_task_id: Optional explicit parent UUID.
        track_gpu: Measure usage from local NVIDIA GPUs for this task. Enable
            this only on the leaf task that owns the GPU work.

    Yields:
        A :class:`TrackedTask` handle.

    Raises:
        RuntimeError: If ``dexcost.init()`` has not been called.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    ctx = get_context()
    merged_metadata = dict(ctx.metadata) if ctx and ctx.metadata else {}
    if metadata:
        merged_metadata.update(metadata)
    with _global_tracker.task(
        task_type=task_type,
        customer_id=ctx.customer_id if ctx else None,
        project_id=ctx.project_id if ctx else None,
        user_id=ctx.user_id if ctx else None,
        product_id=ctx.product_id if ctx else None,
        metadata=merged_metadata,
        experiment_id=experiment_id,
        variant=variant,
        task_id=task_id,
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
        agent_id=ctx.agent if ctx else None,
        agent_version=ctx.agent_version if ctx else None,
        workflow_id=ctx.workflow_id if ctx else None,
        workflow_session_id=ctx.workflow_session_id if ctx else None,
        track_gpu=track_gpu,
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
    idempotency_key: str | None = None,
    capability: CapabilityIdentity | None = None,
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
        idempotency_key=idempotency_key,
        capability=capability,
    )


def report_tool_call(
    tool_id: str,
    *,
    operation: str = "execute",
    status: ToolOperationStatus = "succeeded",
    duration_ms: int = 0,
    usage: ToolUsage | None = None,
    cost_usd: Decimal | str | int = Decimal("0"),
    provider: str | None = None,
    provider_record_id: str | None = None,
    error_type: str | None = None,
    error_code: str | int | None = None,
    dimensions: Mapping[str, ToolDimensionInput] | None = None,
    operation_id: uuid.UUID | str | None = None,
    attempt_id: uuid.UUID | str | None = None,
    attempt_number: int = 1,
    retry_of: uuid.UUID | str | None = None,
    task_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    capability: CapabilityIdentity | None = None,
) -> Event:
    """Manually record one tool call without capturing its inputs or output."""
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    current = get_current_task()
    resolved_task_id = task_id or (current.task_id if current is not None else None)
    if resolved_task_id is None:
        raise RuntimeError(
            "No active task — use dexcost.task() or provide task_id explicitly"
        )
    try:
        resolved_uuid = (
            resolved_task_id
            if isinstance(resolved_task_id, uuid.UUID)
            else uuid.UUID(resolved_task_id)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("task_id must be a valid UUID") from exc
    task_model = (
        current
        if current is not None and current.task_id == resolved_uuid
        else _global_tracker._storage.get_task(str(resolved_uuid))
    )
    if task_model is None:
        # Cross-process capture intentionally does not create or mutate a
        # second task identity; only the event references the canonical ID.
        task_model = Task(task_id=resolved_uuid, task_type="attached")
    return TrackedTask(
        task_model,
        _global_tracker._storage,
        _global_tracker,
    ).record_tool_call(
        tool_id,
        operation=operation,
        status=status,
        duration_ms=duration_ms,
        usage=usage,
        cost_usd=cost_usd,
        provider=provider,
        provider_record_id=provider_record_id,
        error_type=error_type,
        error_code=error_code,
        dimensions=dimensions,
        operation_id=operation_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        retry_of=retry_of,
        idempotency_key=idempotency_key,
        capability=capability,
    )


def track_tool(
    tool_id: str,
    *,
    operation: str = "execute",
    usage: ToolUsage | None = None,
    cost_usd: Decimal | str | int = Decimal("0"),
    provider: str | None = None,
    dimensions: Mapping[str, ToolDimensionInput] | None = None,
    capability: CapabilityIdentity | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a sync, async, generator, or async-generator tool.

    Decoration is safe before :func:`init`; the active tracker is resolved at
    call time. Instrumentation failures never replace the wrapped result or
    exception.
    """

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        def begin() -> object:
            if _global_tracker is None:
                raise RuntimeError("dexcost not initialized — call dexcost.init() first")
            task_model = get_current_task()
            is_auto = task_model is None
            token = None
            if task_model is None:
                from dexcost.tracker import _tool_task_type

                task_model = create_auto_task(_tool_task_type(tool_id))
                token = set_current_task(task_model)
            return (
                _global_tracker,
                task_model,
                token,
                is_auto,
                capability or get_capability(),
            )

        def finish(
            raw_state: object,
            call_status: str,
            duration_ms: int,
            error: BaseException | None,
        ) -> None:
            tracker, task_model, token, is_auto, invocation_capability = cast(
                tuple[
                    CostTracker,
                    Task,
                    contextvars.Token[Task | None] | None,
                    bool,
                    CapabilityIdentity | None,
                ],
                raw_state,
            )
            try:
                event = TrackedTask(
                    task_model,
                    tracker._storage,
                    tracker,
                ).record_tool_call(
                    tool_id,
                    operation=operation,
                    status=cast(ToolOperationStatus, call_status),
                    duration_ms=duration_ms,
                    usage=usage,
                    cost_usd=cost_usd,
                    provider=provider,
                    capability=invocation_capability,
                    error_type=(type(error).__name__ if error is not None else None),
                    dimensions=dimensions,
                )
                if is_auto:
                    finalize_auto_task(
                        task_model,
                        event,
                        status=("success" if call_status == "succeeded" else "failed"),
                    )
                    tracker._storage.insert_task(task_model)
            finally:
                if token is not None:
                    _reset_current_task(token)

        return decorate_tool(function, begin=begin, finish=finish)

    return decorator


def record_outcome(
    name: str,
    *,
    state: OutcomeState = "achieved",
    value: OutcomeInput | OutcomeValue | None = None,
    outcome_id: uuid.UUID | str | None = None,
    revision: int = 1,
    task_id: uuid.UUID | str | None = None,
    effective_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> OutcomeRevision:
    """Append an explicit business outcome for a task.

    When ``task_id`` is omitted, the current :func:`task` context owns the
    outcome. Pass the same ``outcome_id`` with the next revision number when
    correcting or superseding an earlier record.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    current = get_current_task()
    resolved_task_id = task_id or (current.task_id if current is not None else None)
    if resolved_task_id is None:
        raise RuntimeError(
            "No active task — use dexcost.task() or provide task_id explicitly"
        )
    return _global_tracker.record_outcome(
        name,
        task_id=resolved_task_id,
        state=state,
        value=value,
        outcome_id=outcome_id,
        revision=revision,
        effective_at=effective_at,
        observed_at=observed_at,
    )


def get_outcome_history(outcome_id: uuid.UUID | str) -> list[OutcomeRevision]:
    """Return the complete locally retained history for one outcome."""
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    return _global_tracker.get_outcome_history(outcome_id)


def record_revenue(
    amount: RevenueInput | None = None,
    *,
    currency: str = "USD",
    state: RevenueState = "recognized",
    source: RevenueSourceType | RevenueSource = "sdk",
    source_record_id: str | None = None,
    revenue_id: uuid.UUID | str | None = None,
    revision: int = 1,
    task_id: uuid.UUID | str | None = None,
    outcome_id: uuid.UUID | str | None = None,
    effective_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> RevenueRevision:
    """Append explicit revenue; never infer it from task or outcome success.

    When ``task_id`` is omitted, the current :func:`task` context owns the
    revision. Reuse ``revenue_id`` and increment ``revision`` for lifecycle
    changes or corrections.
    """
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    current = get_current_task()
    resolved_task_id = task_id or (current.task_id if current is not None else None)
    if resolved_task_id is None:
        raise RuntimeError(
            "No active task — use dexcost.task() or provide task_id explicitly"
        )
    return _global_tracker.record_revenue(
        amount,
        task_id=resolved_task_id,
        currency=currency,
        state=state,
        source=source,
        source_record_id=source_record_id,
        revenue_id=revenue_id,
        revision=revision,
        outcome_id=outcome_id,
        effective_at=effective_at,
        observed_at=observed_at,
    )


def get_revenue_history(revenue_id: uuid.UUID | str) -> list[RevenueRevision]:
    """Return the complete locally retained history for one revenue record."""
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    return _global_tracker.get_revenue_history(revenue_id)


def set_api_key(new_key: str) -> bool:
    """Update the SDK's API key and resume sync after auth failure.

    Sprint 2 Theme D / §3.2.3 (B14). When the Control Layer returns
    401/403 the SyncWorker permanently stops (sync.py:366-369). Without
    this function the only recovery is restarting the customer's
    process. ``set_api_key`` updates the global config + clears the
    worker's stop signal + restarts the worker thread if it has
    already terminated.

    Returns True on success, False if ``init()`` has not been called
    (logs a warning).
    """
    global _global_config, _sync_worker, _global_tracker, _pricing_engine
    global _catalog_runtime
    if _global_config is None or _global_tracker is None:
        _log.warning(
            "dexcost.set_api_key() called before init(); ignoring. "
            "Call dexcost.init(api_key=...) first."
        )
        return False
    _global_config.api_key = new_key
    if _pricing_engine is not None:
        _pricing_engine.set_api_key(new_key)
    if _catalog_runtime is not None:
        _catalog_runtime.set_api_key(new_key)
    if _sync_worker is None:
        return True  # Local-only mode; nothing else to do.
    # Clear the auth-failed signal and its backoff so delivery can resume.
    _sync_worker.resume_after_auth()
    # If the worker thread already terminated (auth-failure path
    # `return`s from _run), spawn a fresh one. threading.Thread cannot
    # be restarted, so we rebuild the SyncWorker with the same config
    # and storage. The buffered events on disk persist across this
    # transition.
    if _sync_worker._thread is None or not _sync_worker._thread.is_alive():
        from dexcost.storage.sqlite import SQLiteStorage
        sync_storage = SQLiteStorage(db_path=_global_config.buffer_path)
        _sync_worker = SyncWorker(
            config=_global_config,
            storage=sync_storage,
            db_path=_global_config.buffer_path,
        )
        _sync_worker.start()
    return True


def close() -> None:
    """Shut down the global tracker and flush any pending events.

    Safe to call even if ``init()`` has not been called (no-op).
    """
    global _global_tracker, _sync_worker, _pricing_engine, _catalog_runtime
    global _service_catalog_refresh_url, _service_catalog_refresh_api_key
    # Drop credentials first, even if flushing or worker shutdown raises.
    _service_catalog_refresh_url = None
    _service_catalog_refresh_api_key = None
    tracker = _global_tracker
    if _sync_worker is not None:
        _sync_worker.flush()
        _sync_worker.stop()
        _sync_worker = None
    if _catalog_runtime is not None:
        _catalog_runtime.close()
        _catalog_runtime = None
    if _pricing_engine is not None:
        _pricing_engine.close()
    if tracker is not None:
        for instrument_name in tuple(tracker.instrumented):
            try:
                tracker.uninstrument(instrument_name)
            except Exception:
                _log.debug(
                    "failed to remove %s instrumentation during shutdown",
                    instrument_name,
                    exc_info=True,
                )
        try:
            tracker.storage.close()
        except Exception:
            _log.debug("failed to close tracker storage during shutdown", exc_info=True)
    try:
        from dexcost.adapters.http import untrack_http

        untrack_http()
    except Exception:
        _log.debug("failed to remove HTTP instrumentation during shutdown", exc_info=True)
    _global_tracker = None
    _pricing_engine = None


def catalog_status() -> CatalogRuntimeStatus:
    """Return active catalog provenance and last refresh health."""
    if _catalog_runtime is None:
        return CatalogRuntimeStatus(
            release_id=None,
            release_sequence=None,
            source="bootstrap",
            stale=False,
            last_refresh_status=None,
            last_error=None,
        )
    return _catalog_runtime.status()


def import_catalog_bundle(
    bundle: bytes | bytearray | memoryview | str | os.PathLike[str],
) -> CatalogRuntimeStatus:
    """Activate an offline bundle through the configured catalog trust policy."""
    if _catalog_runtime is None:
        raise RuntimeError("dexcost.init() must be called before importing a catalog bundle")
    _catalog_runtime.import_bundle(bundle)
    return _catalog_runtime.status()


def export_catalog_bundle(
    path: str | os.PathLike[str] | None = None,
    *,
    source: str = "active",
) -> bytes:
    """Export the exact active or previous durable catalog release."""
    if _catalog_runtime is None:
        raise RuntimeError("dexcost.init() must be called before exporting a catalog bundle")
    if source not in {"active", "previous"}:
        raise ValueError("catalog bundle source must be active or previous")
    resolved_source = cast(Literal["active", "previous"], source)
    return _catalog_runtime.export_bundle(path, source=resolved_source)


def delivery_status() -> DeliveryStatus:
    """Return transport health joined with durable pending/quarantine depths."""
    storage = None if _global_tracker is None else _global_tracker._storage
    if _sync_worker is None:
        return local_delivery_status(storage)
    return _sync_worker.status(storage)


def explain_pricing(
    event_or_id: Event | uuid.UUID | str,
) -> PricingExplanation:
    """Explain recorded SDK pricing evidence locally, without a network call."""
    if _global_tracker is None:
        raise RuntimeError("dexcost not initialized — call dexcost.init() first")
    return _global_tracker.explain_pricing(event_or_id)


def flush() -> None:
    """Force immediate sync of buffered events to the Control Layer.

    No-op if the SDK is in local-only mode or ``init()`` has not been called.
    """
    if _sync_worker is not None:
        _sync_worker.flush()


__all__ = [
    "ALL_SUPPORTED_INSTRUMENTS",
    "ATTRIBUTION_COMPONENTS",
    "ATTRIBUTION_UNIT_BY_METRIC",
    "ATTRIBUTION_USAGE_METRICS",
    "ATTRIBUTION_USAGE_UNITS",
    "ATTRIBUTION_V2_CONTRACT_VERSION",
    "ATTRIBUTION_V3_CONTRACT_VERSION",
    "AttachedTask",
    "AttributionAttemptIdentityV3",
    "AttributionBillingDimension",
    "AttributionBillingDimensionValue",
    "AttributionCapabilityIdentityV3",
    "AttributionCapabilityInvocationV3",
    "AttributionCapabilityKindV3",
    "AttributionCapabilitySourceV3",
    "AttributionComponent",
    "AttributionConfidence",
    "AttributionCostEvidenceSource",
    "AttributionCostEvidenceV2",
    "AttributionCostEvidenceV3",
    "AttributionEventV2",
    "AttributionEventV3",
    "AttributionLifecycleState",
    "AttributionLifecycleV2",
    "AttributionLifecycleV3",
    "AttributionObservationV3",
    "AttributionOperationErrorV3",
    "AttributionOperationIdentityV3",
    "AttributionOperationStatusV3",
    "AttributionProviderIdentityV2",
    "AttributionProviderIdentityV3",
    "AttributionResourceV2",
    "AttributionResourceV3",
    "AttributionTaskIngestV1",
    "AttributionUsageLineV2",
    "AttributionUsageLineV3",
    "AttributionUsageMetric",
    "AttributionUsageMetricV3",
    "AttributionUsagePeriodV2",
    "AttributionUsagePeriodV3",
    "AttributionUsageUnit",
    "AttributionUsageUnitV3",
    "AttributionV2ValidationIssue",
    "AttributionV2ValidationResult",
    "AttributionV3ValidationIssue",
    "AttributionV3ValidationResult",
    "CapabilityIdentity",
    "CapabilityInvocation",
    "CapabilityKind",
    "CapabilitySource",
    "CatalogRuntimeStatus",
    "CostConfidence",
    "CostResult",
    "CostTracker",
    "DeliveryErrorCallback",
    "DeliveryErrorEvent",
    "DeliveryStatus",
    "DexcostConfig",
    "DexcostContext",
    "Event",
    "EventType",
    "InfrastructureRateEntry",
    "InvalidAPIKeyError",
    "OutcomeInput",
    "OutcomeRevision",
    "OutcomeState",
    "OutcomeValue",
    "OutcomeValueType",
    "PricingEngine",
    "PricingExplanation",
    "PricingExplanationStatus",
    "PricingProvenance",
    "PricingSource",
    "RateEntry",
    "RateRegistry",
    "RevenueAmount",
    "RevenueInput",
    "RevenueRevision",
    "RevenueSource",
    "RevenueSourceType",
    "RevenueState",
    "SchemaNotFoundError",
    "ServiceCatalog",
    "SessionManager",
    "SyncWorker",
    "Task",
    "TaskStatus",
    "ToolDimensionInput",
    "ToolOperationStatus",
    "ToolQuantityInput",
    "ToolUsage",
    "TrackedAnthropic",
    "TrackedOpenAI",
    "TrackedTask",
    "WebhookHeader",
    "WebhookSecret",
    "WebhookVerificationError",
    "__version__",
    "assert_attribution_event_v2",
    "assert_attribution_observation_v3",
    "assert_webhook_signature",
    "async_task_context",
    "attach_task",
    "capability_context",
    "catalog_status",
    "clear_context",
    "close",
    "delivery_status",
    "enforce_metadata_limit",
    "explain_pricing",
    "export_catalog_bundle",
    "flush",
    "get_capability",
    "get_context",
    "get_current_task",
    "get_idempotency_key",
    "get_outcome_history",
    "get_revenue_history",
    "hash_value",
    "idempotency_key",
    "import_catalog_bundle",
    "init",
    "instrument_anthropic",
    "instrument_bedrock",
    "instrument_cohere",
    "instrument_fal",
    "instrument_gemini",
    "instrument_litellm",
    "instrument_mcp",
    "instrument_ollama",
    "instrument_openai",
    "instrument_openrouter",
    "instrument_perplexity",
    "on_delivery_error",
    "record_cost",
    "record_outcome",
    "record_revenue",
    "redact_dict",
    "remove_delivery_error_callback",
    "report_tool_call",
    "set_capability",
    "set_context",
    "set_current_task",
    "set_idempotency_key",
    "task",
    "task_context",
    "to_attribution_event_v2",
    "to_attribution_event_v3",
    "to_attribution_observation_v3",
    "to_attribution_task_ingest_v1",
    "to_business_identity_revision_v1",
    "track_crewai",
    "track_griptape",
    "track_tool",
    "uninstrument_anthropic",
    "uninstrument_bedrock",
    "uninstrument_cohere",
    "uninstrument_fal",
    "uninstrument_gemini",
    "uninstrument_litellm",
    "uninstrument_mcp",
    "uninstrument_ollama",
    "uninstrument_openai",
    "uninstrument_openrouter",
    "uninstrument_perplexity",
    "validate",
    "validate_api_key",
    "validate_attribution_event_v2",
    "validate_attribution_observation_v3",
    "verify_webhook_signature",
]
