"""Handler wraps emit compute_cost events per invocation and pass through
when no dexcost task is in context (capture spec §6 case 2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import dexcost
from dexcost.compute_wrap import wrap_lambda_handler
from dexcost.context import _current_task, set_current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    t = CostTracker(storage=storage, auto_instrument=[])
    monkeypatch.setattr(dexcost, "_global_tracker", t)
    return t


def test_lambda_wrap_emits_event_with_pending_cost(tracker, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024")
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    t = Task(
        task_id=uuid.uuid4(), task_type="lambda",
        started_at=datetime.now(timezone.utc),
    )
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_lambda_handler
    def handler(event, context):
        return {"statusCode": 200}

    try:
        with patch("dexcost.compute_wrap.read_memory_peak",
                   return_value=256 * 1024 * 1024):
            result = handler({}, type("Ctx", (), {})())
    finally:
        _current_task.reset(token)

    assert result == {"statusCode": 200}
    events = tracker.storage.query_events(task_id=str(t.task_id))
    compute = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute) == 1
    details = compute[0].details
    assert details["billing_model"] == "lambda"
    assert details["invocation_count"] == 1
    # 1024 MB → decimal bytes (Lambda env var convention).
    assert details["memory_bytes_limit"] == 1024 * 1_000_000
    assert details["memory_bytes_peak"] == 256 * 1024 * 1024
    assert details["initialization_type"] == "on-demand"
    assert details["region"] == "us-east-1"
    assert details["architecture"] in {"x86_64", "arm64"}
    assert details["cost_pending"] is True
    assert compute[0].cost_usd == 0  # cost back-fills at task finalize


def test_no_active_task_passes_through(monkeypatch):
    """When no dexcost task is in context, wrap is a transparent pass-through."""
    _current_task.set(None)

    @wrap_lambda_handler
    def handler(event, context):
        return "ok"

    assert handler({}, None) == "ok"


def test_handler_exception_still_emits_event(tracker, monkeypatch):
    """capture spec §6 case 7 — Lambda bills failed invocations the same as
    successful ones, so the event must still emit even when the handler
    raises. The exception is re-raised so the customer sees it."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "512")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    t = Task(
        task_id=uuid.uuid4(), task_type="lambda",
        started_at=datetime.now(timezone.utc),
    )
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_lambda_handler
    def handler(event, context):
        raise ValueError("simulated handler failure")

    try:
        with patch("dexcost.compute_wrap.read_memory_peak", return_value=0), \
             pytest.raises(ValueError, match="simulated"):
            handler({}, None)
    finally:
        _current_task.reset(token)

    events = tracker.storage.query_events(task_id=str(t.task_id))
    compute = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute) == 1
    assert compute[0].details["billing_model"] == "lambda"


def test_compute_billing_overrides_threaded_through_init(monkeypatch, tmp_path):
    """init(compute_billing_overrides=...) lands on the global tracker."""
    monkeypatch.setattr(dexcost, "_global_tracker", None)
    monkeypatch.setattr(dexcost, "_sync_worker", None)
    monkeypatch.setattr(dexcost, "_global_config", None)
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "buf.db"),
        track_http=False, auto_instrument=[],
        compute_billing_overrides={"cloud_run": "instance"},
        k8s_node_aware=True,
    )
    tracker = dexcost._global_tracker
    assert tracker is not None
    assert tracker._compute_billing_overrides == {"cloud_run": "instance"}
    assert tracker._k8s_node_aware is True


def test_compute_billing_overrides_defaults_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(dexcost, "_global_tracker", None)
    monkeypatch.setattr(dexcost, "_sync_worker", None)
    monkeypatch.setattr(dexcost, "_global_config", None)
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "buf.db"),
        track_http=False, auto_instrument=[],
    )
    tracker = dexcost._global_tracker
    assert tracker is not None
    assert tracker._compute_billing_overrides == {}
    assert tracker._k8s_node_aware is False


def test_wrap_lambda_handler_exported_from_top_level():
    assert hasattr(dexcost, "wrap_lambda_handler")
    assert hasattr(dexcost, "wrap_cloud_run_handler")
    assert hasattr(dexcost, "wrap_cloud_functions_handler")
    assert hasattr(dexcost, "wrap_azure_functions_handler")
    assert hasattr(dexcost, "wrap_vercel_handler")
