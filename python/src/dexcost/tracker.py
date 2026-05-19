"""CostTracker — task tracking decorator, context manager, manual start/end, and cost aggregation.

Implements US-007 (decorator), US-008 (context manager), US-009 (manual start/end),
US-010 (LLM pricing engine integration), US-011 (cost rates registry),
US-015 (configurable auto-instrumentation), US-016 (non-LLM cost recording),
US-017 (retry detection and waste tracking), and US-033 (trace linking).
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import logging
import threading
import uuid
import warnings
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from dexcost.heuristics import RetryHeuristicEngine

from dexcost.context import async_task_context, get_current_task, set_current_task, task_context
from dexcost.dev_console import is_dev_mode, log_event, log_task_complete
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.pricing import CostResult, PricingEngine
from dexcost.rates import RateRegistry
from dexcost.storage.protocol import StorageBackend

F = TypeVar("F", bound=Callable[..., Any])

_log = logging.getLogger(__name__)

# All SDKs that dexcost knows how to auto-instrument (US-015).
ALL_SUPPORTED_INSTRUMENTS: list[str] = [
    "openai",
    "anthropic",
    "litellm",
    "gemini",
    "bedrock",
    "cohere",
    "mcp",
]

# Transient error types that trigger retry auto-detection (US-017).
TRANSIENT_ERRORS: frozenset[str] = frozenset(
    {"rate_limit", "timeout", "5xx", "server_error", "connection_error"}
)

# Likelihood scores per transient error type used by auto-detection.
_ERROR_LIKELIHOODS: dict[str, float] = {
    "rate_limit": 1.0,
    "timeout": 0.9,
    "5xx": 0.85,
    "server_error": 0.85,
    "connection_error": 0.8,
}


# ---------------------------------------------------------------------------
# TrackedTask — user-facing task handle (US-008, US-009)
# ---------------------------------------------------------------------------


class TrackedTask:
    """A task handle for recording costs, usage, and retries.

    Returned by the context manager interface (US-008) and by
    :meth:`CostTracker.start_task` (US-009)::

        with tracker.task(task_type="resolve_ticket") as task:
            task.record_cost(service="google_maps", cost_usd="0.005")
            task.mark_retry(reason="rate_limit")

        # Or manually (US-009):
        task = tracker.start_task(task_type="resolve_ticket")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.003")
        task.end(status="success")
    """

    def __init__(
        self,
        task: Task,
        storage: StorageBackend,
        tracker: CostTracker,
        *,
        ctx_token: contextvars.Token[Task | None] | None = None,
    ) -> None:
        self._task = task
        self._storage = storage
        self._tracker = tracker
        self._ctx_token = ctx_token
        self._ended = False
        self._lock = threading.Lock()

    @property
    def task_id(self) -> uuid.UUID:
        """The unique identifier for this task."""
        return self._task.task_id

    @property
    def task(self) -> Task:
        """The underlying :class:`Task` dataclass."""
        return self._task

    _NON_LLM_EVENT_TYPES = frozenset({"external_cost", "compute_cost"})

    def record_cost(
        self,
        service: str,
        cost_usd: Decimal | str,
        *,
        event_type: str = "external_cost",
        cost_confidence: str = "exact",
        pricing_source: str = "manual",
        pricing_version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Event:
        """Record a non-LLM cost event for the current task.

        Args:
            service: Name of the external service (e.g. ``"google_maps_api"``).
            cost_usd: Cost in USD (Decimal or string).
            event_type: ``"external_cost"`` (default) or ``"compute_cost"``.
            cost_confidence: One of ``exact``, ``computed``, ``estimated``, ``unknown``.
            pricing_source: Source of pricing data (default ``"manual"``).
            pricing_version: Optional hash referencing the rate snapshot used.
            details: Optional dict of extra metadata (e.g. endpoint, method,
                region, duration_ms).

        Returns:
            The persisted :class:`Event`.

        Raises:
            ValueError: If *event_type* is not ``"external_cost"`` or
                ``"compute_cost"``.
        """
        if event_type not in self._NON_LLM_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(self._NON_LLM_EVENT_TYPES)}, "
                f"got {event_type!r}"
            )
        event = Event(
            task_id=self._task.task_id,
            event_type=event_type,
            cost_usd=Decimal(str(cost_usd)),
            cost_confidence=cost_confidence,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            service_name=service,
            details=details or {},
        )
        self._storage.insert_event(event)
        if is_dev_mode():
            log_event(event, self._task.task_type)
        return event

    def record_usage(
        self,
        service: str,
        units: int | float = 1,
        *,
        details: dict[str, Any] | None = None,
    ) -> Event:
        """Record cost computed from the rate registry.

        Looks up the per-unit rate via :meth:`CostTracker.get_rate` and
        multiplies by *units*.

        Args:
            service: Registered service name.
            units: Number of units consumed (default ``1``).
            details: Optional dict of extra metadata.

        Returns:
            The persisted :class:`Event`.

        Raises:
            ValueError: If no rate is registered for *service*.
        """
        rate = self._tracker.get_rate(service)
        if rate is None:
            raise ValueError(
                f"No rate registered for service {service!r}. "
                f"Use tracker.register_rate(service={service!r}, per=..., cost_usd=...) first."
            )
        cost = rate * Decimal(str(units))
        pricing_version = self._tracker.rate_registry.pricing_version
        event = self.record_cost(
            service=service,
            cost_usd=cost,
            cost_confidence="computed",
            pricing_source="rate_registry",
            pricing_version=pricing_version,
            details=details,
        )
        return event

    def mark_retry(
        self,
        reason: str,
        *,
        cost_usd: Decimal | str = Decimal("0"),
        retry_of: uuid.UUID | None = None,
    ) -> Event:
        """Explicitly flag the current operation as a retry.

        Args:
            reason: Why the retry occurred (e.g. ``"rate_limit"``, ``"timeout"``).
            cost_usd: Additional cost incurred by the retry (default ``0``).
            retry_of: Optional :class:`~uuid.UUID` of the original event this
                retries.

        Returns:
            The persisted retry-marker :class:`Event`.
        """
        event = Event(
            task_id=self._task.task_id,
            event_type="retry_marker",
            cost_usd=Decimal(str(cost_usd)),
            cost_confidence="exact",
            is_retry=True,
            retry_reason=reason,
            retry_of=retry_of,
        )
        self._storage.insert_event(event)
        if is_dev_mode():
            log_event(event, self._task.task_type)
        return event

    def mark_not_retry(self, event_id: uuid.UUID | None = None) -> Event | None:
        """Override a false-positive retry detection on a legitimate repeated call.

        When called without *event_id*, the most recent retry event for this
        task is unflagged.  Returns the updated :class:`Event`, or ``None`` if
        no retry event was found.

        Args:
            event_id: Specific event to unflag.  When ``None`` the most
                recent retry-flagged event in this task is used.

        Returns:
            The updated :class:`Event`, or ``None`` if nothing to unflag.
        """
        events = self._storage.query_events(task_id=str(self._task.task_id))
        events.sort(key=lambda e: e.occurred_at, reverse=True)

        target: Event | None = None
        if event_id is not None:
            target = next((e for e in events if e.event_id == event_id), None)
        else:
            # Most recent retry event (query_events returns DESC order)
            target = next((e for e in events if e.is_retry), None)

        if target is None:
            return None

        target.is_retry = False
        target.retry_reason = None
        target.retry_of = None
        self._storage.update_event(target)
        return target

    def link_trace(self, provider: str, trace_id: str) -> None:
        """Link an external trace (Langfuse, LangSmith, etc.) to this task.

        Stores the trace reference in the task's ``metadata["_trace_links"]``
        list.  Multiple traces from different providers can be linked.

        Args:
            provider: Name of the observability platform (e.g. ``"langfuse"``).
            trace_id: The trace or run identifier from the external platform.
        """
        links: list[dict[str, str]] = self._task.metadata.setdefault("_trace_links", [])
        links.append({"provider": provider, "trace_id": trace_id})

    def get_trace_links(self) -> list[dict[str, str]]:
        """Return all linked traces for this task.

        Returns:
            A list of dicts with ``"provider"`` and ``"trace_id"`` keys.
        """
        return cast("list[dict[str, str]]", self._task.metadata.get("_trace_links", []))

    def record_llm_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal | str | None = None,
        *,
        cost_confidence: str | None = None,
        pricing_source: str | None = None,
        cached_tokens: int = 0,
        latency_ms: int | None = None,
        details: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> Event:
        """Record an LLM call event for the current task.

        When *cost_usd* is ``None``, the cost is auto-computed via the
        :class:`~dexcost.pricing.PricingEngine` (US-010).

        If a prior LLM call for the same model within this task ended in a
        transient error (rate_limit, timeout, 5xx) within the configurable
        retry window, the event is automatically flagged as a retry (US-017).

        Args:
            provider: LLM provider name (e.g. ``"openai"``).
            model: Model identifier (e.g. ``"gpt-4"``).
            input_tokens: Number of input tokens consumed.
            output_tokens: Number of output tokens generated.
            cost_usd: Cost in USD (Decimal or string).  When ``None`` the
                pricing engine calculates the cost automatically.
            cost_confidence: Confidence level.  Auto-set when cost is computed.
            pricing_source: Source of pricing data.  Auto-set when cost is
                computed.
            cached_tokens: Number of cached tokens (default ``0``).
            latency_ms: Response latency in milliseconds (optional).
            details: Optional dict of extra metadata.
            error_type: Transient error that caused this call to fail, e.g.
                ``"rate_limit"``, ``"timeout"``, ``"5xx"``.  Stored in
                ``details["error_type"]`` and participates in auto-detection
                for subsequent calls.

        Returns:
            The persisted :class:`Event`.
        """
        pricing_version: str | None = None

        if cost_usd is None:
            # Auto-compute via pricing engine (US-010)
            result = self._tracker._pricing.get_cost(
                model, input_tokens, output_tokens, cached_tokens
            )
            final_cost = result.cost_usd
            cost_confidence = cost_confidence or result.cost_confidence
            pricing_source = pricing_source or result.pricing_source
            pricing_version = result.pricing_version
        else:
            final_cost = Decimal(str(cost_usd))
            cost_confidence = cost_confidence or "exact"
            pricing_source = pricing_source or "manual"

        final_details = dict(details or {})
        if error_type is not None:
            final_details["error_type"] = error_type

        event = Event(
            task_id=self._task.task_id,
            event_type="llm_call",
            cost_usd=final_cost,
            cost_confidence=cost_confidence,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            details=final_details,
        )

        # Retry auto-detection — choose between US-036 heuristic engine
        # (in-memory, confidence-scored) and US-017 storage-based fallback.
        engine = self._tracker._heuristic_engine
        if engine is not None:
            # US-036: advanced heuristic retry detection (in-memory engine)
            if not event.is_retry:
                match = engine.check(event)
                if match.is_retry:
                    event.is_retry = True
                    event.retry_reason = match.reason
                    event.retry_of = match.matched_event_id
                    event.details = dict(event.details)
                    event.details["retry_confidence"] = match.confidence
        elif self._tracker._enable_retry_heuristics:
            # US-017 fallback: storage-based detection (no engine)
            prior = self._detect_retry(model, event.occurred_at)
            if prior is not None:
                event.is_retry = True
                event.retry_reason = prior.details.get("error_type", "unknown")
                event.retry_of = prior.event_id

        self._storage.insert_event(event)
        if is_dev_mode():
            log_event(event, self._task.task_type)

        # Feed the stored event into the heuristic engine's sliding window
        if engine is not None:
            engine.record(event)

        return event

    def _detect_retry(self, model: str, timestamp: datetime) -> Event | None:
        """Check if the current LLM call is likely a retry of a prior failed call.

        Looks at the **most recent** LLM call event for the same model in this
        task.  If that event has a transient ``error_type`` in its details and
        falls within the configured retry window, this call is flagged as a
        retry.  A successful call in between breaks the chain — only the
        immediately preceding event matters.

        Returns the prior event if the computed likelihood meets the threshold,
        otherwise ``None``.
        """
        threshold = self._tracker._retry_likelihood_threshold
        window = self._tracker._retry_window_seconds
        cutoff = timestamp - timedelta(seconds=window)

        # query_events returns events in descending timestamp order
        events = self._storage.query_events(task_id=str(self._task.task_id))

        for event in events:
            if event.occurred_at < cutoff:
                break
            if event.event_type != "llm_call":
                continue
            if event.model != model:
                continue

            # Found the most recent LLM call for the same model.
            # Only this event determines whether the current call is a retry.
            error_type = event.details.get("error_type")
            if error_type is not None and error_type in TRANSIENT_ERRORS:
                likelihood = _ERROR_LIKELIHOODS.get(error_type, 0.8)
                if likelihood >= threshold:
                    return event
            return None

        return None

    def end(self, status: str = "success") -> None:
        """Close the task, compute aggregates, and persist to storage.

        Must be called exactly once for tasks created via
        :meth:`CostTracker.start_task`.

        Args:
            status: Final task status — ``"success"`` or ``"failed"``.

        Raises:
            RuntimeError: If called more than once.
        """
        with self._lock:
            if self._ended:
                raise RuntimeError(f"Task {self._task.task_id} has already been ended.")
            self._ended = True

        self._task.status = status
        if status == "failed":
            self._task.failure_count = 1
        self._task.ended_at = datetime.now(timezone.utc)
        self._tracker._aggregate_costs(self._task)
        self._storage.update_task(self._task)
        if is_dev_mode():
            log_task_complete(self._task)

        if self._ctx_token is not None:
            from dexcost.context import _current_task

            _current_task.reset(self._ctx_token)

    def __del__(self) -> None:
        if not self._ended:
            warnings.warn(
                f"Task {self._task.task_id} was garbage-collected without "
                f".end() being called. Always call task.end() for tasks "
                f"created via tracker.start_task().",
                ResourceWarning,
                stacklevel=2,
            )


# ---------------------------------------------------------------------------
# _TaskContextManager — dual sync/async context manager (US-008)
# ---------------------------------------------------------------------------


class _TaskContextManager:
    """Supports both ``with`` and ``async with`` usage."""

    def __init__(
        self,
        tracker: CostTracker,
        task_type: str,
        customer_id: str | None,
        project_id: str | None,
        metadata: dict[str, Any],
        experiment_id: str | None = None,
        variant: str | None = None,
    ) -> None:
        self._tracker = tracker
        self._task_type = task_type
        self._customer_id = customer_id
        self._project_id = project_id
        self._metadata = metadata
        self._experiment_id = experiment_id
        self._variant = variant
        self._tracked: TrackedTask | None = None
        self._token: contextvars.Token[Task | None] | None = None

    def _setup(self) -> TrackedTask:
        task = Task(
            task_type=self._task_type,
            customer_id=self._customer_id,
            project_id=self._project_id,
            metadata=copy.deepcopy(self._metadata) if self._metadata else {},
            experiment_id=self._experiment_id,
            variant=self._variant,
        )

        parent = get_current_task()
        if parent is not None and task.parent_task_id is None:
            task.parent_task_id = parent.task_id

        self._tracker._storage.insert_task(task)

        self._token = set_current_task(task)
        tracked = TrackedTask(task, self._tracker._storage, self._tracker)
        self._tracked = tracked
        return tracked

    def _teardown(
        self,
        exc_type: type[BaseException] | None,
    ) -> None:
        assert self._tracked is not None
        assert self._token is not None

        task = self._tracked._task
        if exc_type is not None:
            task.status = "failed"
            task.failure_count = 1
        else:
            task.status = "success"

        task.ended_at = datetime.now(timezone.utc)
        self._tracker._aggregate_costs(task)
        self._tracker._storage.update_task(task)
        if is_dev_mode():
            log_task_complete(task)

        from dexcost.context import _current_task

        _current_task.reset(self._token)

        # Mark as ended so __del__ doesn't warn for context-managed tasks
        self._tracked._ended = True

    # Sync context manager protocol
    def __enter__(self) -> TrackedTask:
        return self._setup()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._teardown(exc_type)

    # Async context manager protocol
    async def __aenter__(self) -> TrackedTask:
        return self._setup()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._teardown(exc_type)


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


class CostTracker:
    """High-level tracker: decorator, context manager, manual start/end, and cost aggregation.

    Usage (decorator — US-007)::

        tracker = CostTracker(storage=SQLiteStorage("/tmp/buffer.db"))

        @tracker.track_task(task_type="resolve_ticket", customer_id="acme")
        def my_agent(prompt: str) -> str:
            ...

    Usage (context manager — US-008)::

        with tracker.task(task_type="resolve_ticket") as task:
            task.record_cost(service="google_maps", cost_usd="0.005")

    Usage (manual start/end — US-009)::

        task = tracker.start_task(task_type="resolve_ticket", customer_id="acme")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.003")
        task.end(status="success")
    """

    def __init__(
        self,
        storage: StorageBackend | None = None,
        *,
        pricing: PricingEngine | None = None,
        pricing_data_path: str | Path | None = None,
        auto_update_pricing: bool = False,
        auto_instrument: Sequence[str] | None = None,
        retry_window_seconds: int = 30,
        retry_likelihood_threshold: float = 0.8,
        enable_retry_heuristics: bool = False,
        retry_heuristic_window: float | None = None,
        retry_heuristic_threshold: float | None = None,
    ) -> None:
        if storage is None:
            from dexcost.storage.sqlite import SQLiteStorage

            storage = SQLiteStorage()
        self._storage: StorageBackend = storage
        self._rate_registry = RateRegistry()

        # Retry detection configuration (US-017)
        # PRD: v1.0 supports manual tagging + explicit exceptions only.
        # Heuristic (threshold-based) detection is opt-in.
        self._retry_window_seconds = retry_window_seconds
        self._retry_likelihood_threshold = retry_likelihood_threshold
        self._enable_retry_heuristics = enable_retry_heuristics

        # Advanced retry heuristic engine (US-036)
        self._heuristic_engine: RetryHeuristicEngine | None = None
        if enable_retry_heuristics:
            from dexcost.heuristics import RetryHeuristicEngine

            heuristic_window = (
                retry_heuristic_window
                if retry_heuristic_window is not None
                else float(retry_window_seconds)
            )
            heuristic_threshold = (
                retry_heuristic_threshold
                if retry_heuristic_threshold is not None
                else retry_likelihood_threshold
            )
            self._heuristic_engine = RetryHeuristicEngine(
                window_seconds=heuristic_window,
                threshold=heuristic_threshold,
            )

        # LLM pricing engine (US-010)
        if pricing is not None:
            self._pricing = pricing
        else:
            self._pricing = PricingEngine(
                data_path=pricing_data_path,
                auto_update=auto_update_pricing,
            )

        # Configurable auto-instrumentation (US-015)
        self._instrumented: set[str] = set()
        if auto_instrument is None:
            auto_instrument = list(ALL_SUPPORTED_INSTRUMENTS)
        for name in auto_instrument:
            try:
                self.instrument(name)
            except ImportError:
                _log.debug("SDK %r not installed, skipping auto-instrumentation", name)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                _log.debug("SDK %r failed to instrument, skipping", name, exc_info=True)

    @property
    def storage(self) -> StorageBackend:
        """The underlying storage backend."""
        return self._storage

    @property
    def pricing(self) -> PricingEngine:
        """The underlying :class:`PricingEngine`."""
        return self._pricing

    # ------------------------------------------------------------------
    # Auto-instrumentation (US-015)
    # ------------------------------------------------------------------

    def instrument(self, name: str) -> None:
        """Instrument a specific SDK by name.

        Can be called after init for lazy instrumentation::

            tracker = CostTracker(auto_instrument=[])
            tracker.instrument("openai")

        Args:
            name: SDK name — one of ``"openai"``, ``"anthropic"``, ``"litellm"``,
                ``"gemini"``, ``"bedrock"``, ``"cohere"``.

        Raises:
            ValueError: If *name* is not a supported SDK.
            ImportError: If the SDK package is not installed.
            RuntimeError: If the SDK is already instrumented.
        """
        if name not in ALL_SUPPORTED_INSTRUMENTS:
            raise ValueError(
                f"Unsupported instrument name {name!r}. " f"Supported: {ALL_SUPPORTED_INSTRUMENTS}"
            )

        if name in self._instrumented:
            raise RuntimeError(
                f"SDK {name!r} is already instrumented on this tracker. "
                f"Call tracker.uninstrument({name!r}) first."
            )

        if name == "openai":
            from dexcost.instruments.openai import instrument_openai

            instrument_openai(self)
        elif name == "anthropic":
            from dexcost.instruments.anthropic import instrument_anthropic

            instrument_anthropic(self)
        elif name == "litellm":
            from dexcost.instruments.litellm import instrument_litellm

            instrument_litellm(self)
        elif name == "gemini":
            from dexcost.instruments.gemini import instrument_gemini

            instrument_gemini(self)
        elif name == "bedrock":
            from dexcost.instruments.bedrock import instrument_bedrock

            instrument_bedrock(self)
        elif name == "cohere":
            from dexcost.instruments.cohere import instrument_cohere

            instrument_cohere(self)
        elif name == "mcp":
            from dexcost.instruments.mcp import instrument_mcp

            instrument_mcp(self)

        self._instrumented.add(name)

    def uninstrument(self, name: str) -> None:
        """Remove instrumentation for a specific SDK.

        Safe to call even if the SDK is not currently instrumented (no-op).

        Args:
            name: SDK name — one of ``"openai"``, ``"anthropic"``, ``"litellm"``,
                ``"gemini"``, ``"bedrock"``, ``"cohere"``.

        Raises:
            ValueError: If *name* is not a supported SDK.
        """
        if name not in ALL_SUPPORTED_INSTRUMENTS:
            raise ValueError(
                f"Unsupported instrument name {name!r}. " f"Supported: {ALL_SUPPORTED_INSTRUMENTS}"
            )

        if name not in self._instrumented:
            return

        if name == "openai":
            from dexcost.instruments.openai import uninstrument_openai

            uninstrument_openai()
        elif name == "anthropic":
            from dexcost.instruments.anthropic import uninstrument_anthropic

            uninstrument_anthropic()
        elif name == "litellm":
            from dexcost.instruments.litellm import uninstrument_litellm

            uninstrument_litellm()
        elif name == "gemini":
            from dexcost.instruments.gemini import uninstrument_gemini

            uninstrument_gemini()
        elif name == "bedrock":
            from dexcost.instruments.bedrock import uninstrument_bedrock

            uninstrument_bedrock()
        elif name == "cohere":
            from dexcost.instruments.cohere import uninstrument_cohere

            uninstrument_cohere()
        elif name == "mcp":
            from dexcost.instruments.mcp import uninstrument_mcp

            uninstrument_mcp()

        self._instrumented.discard(name)

    @property
    def instrumented(self) -> frozenset[str]:
        """The set of currently instrumented SDK names."""
        return frozenset(self._instrumented)

    # ------------------------------------------------------------------
    # LLM pricing (US-010)
    # ------------------------------------------------------------------

    def get_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> CostResult:
        """Calculate the cost for an LLM call.

        Convenience wrapper around :meth:`PricingEngine.get_cost`.
        """
        return self._pricing.get_cost(model, input_tokens, output_tokens, cached_tokens)

    def set_custom_pricing(
        self,
        model: str,
        input_per_1k: Decimal | str | float,
        output_per_1k: Decimal | str | float,
    ) -> None:
        """Register custom per-token pricing for a model.

        See :meth:`PricingEngine.set_custom_pricing` for details.
        """
        self._pricing.set_custom_pricing(model, input_per_1k, output_per_1k)

    # ------------------------------------------------------------------
    # Rate registry (US-011)
    # ------------------------------------------------------------------

    @property
    def rate_registry(self) -> RateRegistry:
        """The underlying :class:`RateRegistry`."""
        return self._rate_registry

    def register_rate(
        self,
        service: str,
        *,
        per: str = "unit",
        cost_usd: Decimal | str,
    ) -> None:
        """Register a per-unit cost rate for *service*.

        Once registered, :meth:`TrackedTask.record_usage` can compute costs
        automatically without specifying ``cost_usd`` each time.

        Args:
            service: Service identifier (e.g. ``"maps.googleapis.com"``).
            per: What a "unit" means (e.g. ``"request"``, ``"page"``).
            cost_usd: Cost per unit in USD.
        """
        self._rate_registry.register(service, per, cost_usd)

    def get_rate(self, service: str) -> Decimal | None:
        """Return the registered per-unit rate for *service*, or ``None``."""
        entry = self._rate_registry.get(service)
        return entry.cost_usd if entry is not None else None

    def load_rates(self, path: str | Path) -> None:
        """Load rates from a YAML config file.

        See :meth:`RateRegistry.load` for the expected YAML format.

        Args:
            path: Path to the YAML file.
        """
        self._rate_registry.load(path)

    def export_rates(self, path: str | Path) -> None:
        """Export current rates to a YAML config file.

        The output is deterministically sorted by service name so that
        the file is suitable for version control.

        Args:
            path: Path to write the YAML file.
        """
        self._rate_registry.export(path)

    # ------------------------------------------------------------------
    # Manual start/end (US-009)
    # ------------------------------------------------------------------

    def start_task(
        self,
        task_type: str = "",
        customer_id: str | None = None,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        experiment_id: str | None = None,
        variant: str | None = None,
    ) -> TrackedTask:
        """Manually start a task and return a :class:`TrackedTask` handle.

        Use this when decorators and context managers don't fit your
        architecture (e.g. Celery workers, multi-process pipelines).
        The caller **must** call :meth:`TrackedTask.end` when the task
        is complete.

        Args:
            task_type: Identifier for the kind of task (e.g. ``"resolve_ticket"``).
            customer_id: Optional customer attribution.
            project_id: Optional project attribution.
            metadata: Optional dict of extra metadata.
            experiment_id: Optional experiment grouping.
            variant: Optional variant label within experiment.

        Returns:
            A :class:`TrackedTask` whose ``task_id`` can be passed to other
            functions or processes for manual event association.
        """
        task = Task(
            task_type=task_type,
            customer_id=customer_id,
            project_id=project_id,
            metadata=copy.deepcopy(metadata) if metadata else {},
            experiment_id=experiment_id,
            variant=variant,
        )

        parent = get_current_task()
        if parent is not None and task.parent_task_id is None:
            task.parent_task_id = parent.task_id

        self._storage.insert_task(task)

        ctx_token = set_current_task(task)
        return TrackedTask(task, self._storage, self, ctx_token=ctx_token)

    # ------------------------------------------------------------------
    # Context manager (US-008)
    # ------------------------------------------------------------------

    def task(
        self,
        task_type: str = "",
        customer_id: str | None = None,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        experiment_id: str | None = None,
        variant: str | None = None,
    ) -> _TaskContextManager:
        """Return a context manager for explicit task tracking.

        Supports both sync and async usage::

            with tracker.task(task_type="gen_report") as task:
                ...

            async with tracker.task(task_type="gen_report") as task:
                ...

        The yielded :class:`TrackedTask` exposes ``record_cost``,
        ``record_usage``, ``mark_retry``, and ``task_id``.
        """
        return _TaskContextManager(
            tracker=self,
            task_type=task_type,
            customer_id=customer_id,
            project_id=project_id,
            metadata=copy.deepcopy(metadata) if metadata else {},
            experiment_id=experiment_id,
            variant=variant,
        )

    # ------------------------------------------------------------------
    # Decorator (US-007)
    # ------------------------------------------------------------------

    def track_task(
        self,
        task_type: str = "",
        customer_id: str | None = None,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        experiment_id: str | None = None,
        variant: str | None = None,
    ) -> Callable[[F], F]:
        """Decorator that wraps *func* with automatic task tracking.

        Creates a :class:`Task`, sets it as the current task via contextvars,
        runs the function, aggregates costs from recorded events, and persists
        the final task state.
        """
        meta = copy.deepcopy(metadata) if metadata else {}

        def decorator(func: F) -> F:
            if asyncio.iscoroutinefunction(func):

                @functools.wraps(func)
                async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    task = Task(
                        task_type=task_type,
                        customer_id=customer_id,
                        project_id=project_id,
                        metadata=copy.deepcopy(meta) if meta else {},
                        experiment_id=experiment_id,
                        variant=variant,
                    )
                    parent = get_current_task()
                    if parent is not None and task.parent_task_id is None:
                        task.parent_task_id = parent.task_id
                    self._storage.insert_task(task)
                    async with async_task_context(task):
                        try:
                            result = await func(*args, **kwargs)
                            task.status = "success"
                            return result
                        except BaseException:
                            task.status = "failed"
                            task.failure_count = 1
                            raise
                        finally:
                            task.ended_at = datetime.now(timezone.utc)
                            self._aggregate_costs(task)
                            self._storage.update_task(task)

                return cast(F, _async_wrapper)

            @functools.wraps(func)
            def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                task = Task(
                    task_type=task_type,
                    customer_id=customer_id,
                    project_id=project_id,
                    metadata=copy.deepcopy(meta) if meta else {},
                    experiment_id=experiment_id,
                    variant=variant,
                )
                parent = get_current_task()
                if parent is not None and task.parent_task_id is None:
                    task.parent_task_id = parent.task_id
                self._storage.insert_task(task)
                with task_context(task):
                    try:
                        result = func(*args, **kwargs)
                        task.status = "success"
                        return result
                    except BaseException:
                        task.status = "failed"
                        task.failure_count = 1
                        raise
                    finally:
                        task.ended_at = datetime.now(timezone.utc)
                        self._aggregate_costs(task)
                        self._storage.update_task(task)

            return cast(F, _sync_wrapper)

        return decorator

    # ------------------------------------------------------------------
    # Cost aggregation
    # ------------------------------------------------------------------

    def _aggregate_costs(self, task: Task) -> None:
        """Query events for *task* and roll up aggregated cost fields."""
        events = self._storage.query_events(task_id=str(task.task_id))

        task.llm_cost_usd = Decimal("0")
        task.external_cost_usd = Decimal("0")
        task.compute_cost_usd = Decimal("0")
        task.total_cost_usd = Decimal("0")
        task.total_input_tokens = 0
        task.total_output_tokens = 0
        task.total_cached_tokens = 0
        task.retry_count = 0
        task.retry_cost_usd = Decimal("0")

        for event in events:
            if event.event_type == "llm_call":
                task.llm_cost_usd += event.cost_usd
                task.total_input_tokens += event.input_tokens or 0
                task.total_output_tokens += event.output_tokens or 0
                task.total_cached_tokens += event.cached_tokens or 0
            elif event.event_type == "external_cost":
                task.external_cost_usd += event.cost_usd
            elif event.event_type == "compute_cost":
                task.compute_cost_usd += event.cost_usd

            if event.is_retry:
                task.retry_count += 1
                task.retry_cost_usd += event.cost_usd

            task.total_cost_usd += event.cost_usd

        # Network capture — finalize the in-process accountant onto the task.
        net = task._network.finalize()
        task.network_bytes_in = net["bytes_in"]
        task.network_bytes_out = net["bytes_out"]
        task.network_call_count = net["call_count"]
        task.network_by_host = net["by_host"]
