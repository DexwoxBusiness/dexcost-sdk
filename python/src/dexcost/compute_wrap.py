"""Serverless handler wraps for compute capture.

Each wrap is a thin decorator that:
  1. Reads runtime-specific env vars (memory limit, init type, region).
  2. Creates a :class:`ComputeAccountant` and attaches it to the active task.
  3. Times the handler with ``time.monotonic_ns``.
  4. Reads ``cgroup memory.peak`` at exit.
  5. Builds the per-invocation ``compute_cost`` event with ``cost_pending=true``
     and persists it via the global tracker's storage.
  6. ``_aggregate_costs`` back-fills the dollar at task finalize.

When no dexcost task is in context the wrap is a transparent pass-through —
anonymous compute never creates orphan events (capture spec §6 case 2).
"""

from __future__ import annotations

import functools
import logging
import os
import time
import uuid
from typing import Any, Callable

from dexcost.cgroup_reader import read_memory_peak
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.context import get_current_task
from dexcost.models.event import Event

_log = logging.getLogger(__name__)


def _persist_compute_event(task, details: dict[str, Any]) -> None:
    """Insert the compute_cost event with cost_pending=true via the global
    tracker's storage. Tracker.init() back-fills cost_usd at task finalize."""
    try:
        from dexcost import _global_tracker  # type: ignore[attr-defined]
    except ImportError:
        return
    if _global_tracker is None:
        return
    ev = Event(
        event_id=uuid.uuid4(),
        task_id=task.task_id,
        event_type="compute_cost",
        cost_usd=__import__("decimal").Decimal("0"),
        details=details,
    )
    try:
        _global_tracker.storage.insert_event(ev)
    except Exception:  # noqa: BLE001 — fail-silent per convention §9
        _log.warning("compute_wrap failed to persist event", exc_info=True)


def _time_and_capture(
    accountant: ComputeAccountant,
    handler: Callable[..., Any],
    args: tuple,
    kwargs: dict,
) -> Any:
    """Run ``handler`` while measuring duration_ms and peak memory; persist a
    serverless compute_cost event on exit. Exceptions from the handler are
    re-raised after the event is persisted (the cost is still incurred)."""
    t0 = time.monotonic_ns()
    try:
        return handler(*args, **kwargs)
    finally:
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        peak = read_memory_peak() or 0
        try:
            details = accountant.build_serverless_event(
                duration_ms=int(duration_ms),
                memory_bytes_peak=int(peak),
            )
            task = get_current_task()
            if details is not None and task is not None:
                _persist_compute_event(task, details)
        except Exception:  # noqa: BLE001 — fail-silent
            _log.warning(
                "compute_wrap event-build failed", exc_info=True,
            )


# ─── Lambda ──────────────────────────────────────────────────────────────────


def wrap_lambda_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an AWS Lambda handler — emits one compute_cost event per
    invocation with the env-declared memory limit, the wall-clock duration,
    the architecture detected at runtime, and the initialization type."""

    @functools.wraps(fn)
    def _wrapped(event, context, /, *args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(event, context, *args, **kwargs)
        try:
            mem_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "128"))
        except (TypeError, ValueError):
            mem_mb = 128
        init_type = os.environ.get(
            "AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand",
        )
        region = os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION",
        )
        accountant = ComputeAccountant(
            runtime=RuntimeKind.LAMBDA,
            lambda_memory_mb=mem_mb,
            initialization_type=init_type,
            region=region,
        )
        task._compute = accountant
        return _time_and_capture(accountant, fn, (event, context, *args), kwargs)

    return _wrapped


# ─── Cloud Run ───────────────────────────────────────────────────────────────


def wrap_cloud_run_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a Cloud Run HTTP handler. Default billing model is request-based
    (estimated confidence); override via init(compute_billing_overrides=
    {'cloud_run': 'instance'}) for instance-based billing customers."""

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = ComputeAccountant(
            runtime=RuntimeKind.CLOUD_RUN,
            region=_gcp_region_from_env(),
        )
        task._compute = accountant
        return _time_and_capture(accountant, fn, args, kwargs)

    return _wrapped


# ─── Cloud Functions Gen2 ────────────────────────────────────────────────────


def wrap_cloud_functions_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = ComputeAccountant(
            runtime=RuntimeKind.CLOUD_FUNCTIONS,
            region=_gcp_region_from_env(),
        )
        task._compute = accountant
        return _time_and_capture(accountant, fn, args, kwargs)

    return _wrapped


# ─── Azure Functions ─────────────────────────────────────────────────────────


def wrap_azure_functions_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = ComputeAccountant(
            runtime=RuntimeKind.AZURE_FUNCTIONS,
            region=os.environ.get("REGION_NAME"),
        )
        task._compute = accountant
        return _time_and_capture(accountant, fn, args, kwargs)

    return _wrapped


# ─── Vercel Fluid ────────────────────────────────────────────────────────────


def wrap_vercel_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = ComputeAccountant(
            runtime=RuntimeKind.VERCEL,
            region=os.environ.get("VERCEL_REGION"),
        )
        task._compute = accountant
        return _time_and_capture(accountant, fn, args, kwargs)

    return _wrapped


# ─── Helpers ────────────────────────────────────────────────────────────────


def _gcp_region_from_env() -> str | None:
    """GCP region is not exposed via env vars on Cloud Run / Cloud Functions
    Gen2 — it comes from cloud_detect's Phase 2 IMDS probe. Returns None here;
    the pricing engine will fall through to provider defaults if the probe
    hasn't landed yet."""
    from dexcost.cloud_detect import get_cloud_env
    return get_cloud_env().region
