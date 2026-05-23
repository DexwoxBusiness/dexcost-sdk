"""Serverless GPU handler wraps — Modal / RunPod / Replicate.

Phase 2 GPU foundation Task 7. Mirrors compute_wrap.py's shape — per-runtime
decorator that creates a GpuAccountant, times the handler, persists the
dual events (one gpu_cost with cost_pending=true + N gpu_utilization_signal
events) at exit.

When no dexcost task is in context the wrap is transparent (capture spec
§6 case 2 — anonymous compute never creates orphan events).

Handler exceptions are re-raised AFTER the events are persisted because
the GPU-seconds were consumed regardless — Modal et al. bill the failed
invocation the same as a successful one (capture spec §6 case 7).
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Callable

from dexcost.cloud_detect import get_cloud_env
from dexcost.context import get_current_task
from dexcost.gpu_accountant import GpuAccountant
from dexcost.gpu_runtime import GpuRuntimeKind
from dexcost.models.enums import EventType
from dexcost.models.event import Event

_log = logging.getLogger(__name__)


def _persist_gpu_events(task, cost_details: dict[str, Any] | None,
                        signal_events: list[dict[str, Any]] | None) -> None:
    """Insert gpu_cost (cost_pending=true) + gpu_utilization_signal events."""
    try:
        from dexcost import _global_tracker  # type: ignore[attr-defined]
    except ImportError:
        return
    if _global_tracker is None or task is None:
        return
    try:
        if cost_details is not None:
            ev = Event(
                event_id=uuid.uuid4(),
                task_id=task.task_id,
                event_type=EventType.GPU_COST.value,
                cost_usd=Decimal("0"),  # back-filled at task finalize
                details=cost_details,
            )
            _global_tracker.storage.insert_event(ev)
        if signal_events:
            for sig_details in signal_events:
                ev = Event(
                    event_id=uuid.uuid4(),
                    task_id=task.task_id,
                    event_type=EventType.GPU_UTILIZATION_SIGNAL.value,
                    cost_usd=Decimal("0"),  # Decision #3 — observability only
                    details=sig_details,
                )
                _global_tracker.storage.insert_event(ev)
    except Exception:  # noqa: BLE001 — fail-silent per convention §9
        _log.warning("gpu_wrap failed to persist events", exc_info=True)


def _time_and_capture(
    accountant: GpuAccountant,
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
) -> Any:
    """Run ``fn`` while sampling NVML; persist dual events on exit.

    Handler exceptions are re-raised after events are persisted — the
    GPU-seconds were consumed and Modal/RunPod/Replicate bill failed
    invocations identically to successful ones.
    """
    accountant.snapshot_start()
    t0_ns = time.monotonic_ns()
    try:
        return fn(*args, **kwargs)
    finally:
        duration_ms = (time.monotonic_ns() - t0_ns) // 1_000_000
        try:
            cost_details, sigs = accountant.snapshot_end_and_build(
                duration_ms=int(duration_ms),
            )
            task = get_current_task()
            if task is not None:
                _persist_gpu_events(task, cost_details, sigs)
        except Exception:  # noqa: BLE001 — fail-silent
            _log.warning("gpu_wrap event-build failed", exc_info=True)


# ─── Modal ───────────────────────────────────────────────────────────────────


def wrap_modal_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a Modal handler — emits 1 gpu_cost + N gpu_utilization_signal events.

    The handler must be invoked inside an active dexcost task; otherwise
    the wrap is a transparent pass-through (capture spec §6 case 2).
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = GpuAccountant(GpuRuntimeKind.MODAL, get_cloud_env())
        task._gpu = accountant
        return _time_and_capture(accountant, fn, args, kwargs)
    return _wrapped


# ─── RunPod ──────────────────────────────────────────────────────────────────


def wrap_runpod_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = GpuAccountant(GpuRuntimeKind.RUNPOD, get_cloud_env())
        task._gpu = accountant
        return _time_and_capture(accountant, fn, args, kwargs)
    return _wrapped


# ─── Replicate ───────────────────────────────────────────────────────────────


def wrap_replicate_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        task = get_current_task()
        if task is None:
            return fn(*args, **kwargs)
        accountant = GpuAccountant(GpuRuntimeKind.REPLICATE, get_cloud_env())
        task._gpu = accountant
        return _time_and_capture(accountant, fn, args, kwargs)
    return _wrapped
