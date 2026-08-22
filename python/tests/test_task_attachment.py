"""Non-owning cross-process canonical task attachment."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import dexcost
from dexcost.context import get_current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import AttachedTask, CostTracker


def test_attachment_records_without_creating_or_ending_remote_task(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "attach.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task_id = uuid.uuid4()
    attached = tracker.attach_task(task_id)
    try:
        assert isinstance(attached, AttachedTask)
        event = attached.record_cost("remote-service", "0.01")
        outcome = attached.record_outcome("customer_notified", value=True)
        revenue = attached.record_revenue("12", source_record_id="invoice-1")

        assert event.task_id == outcome.task_id == revenue.task_id == task_id
        assert storage.get_task(str(task_id)) is None
        with pytest.raises(RuntimeError, match="cannot end"):
            attached.end()
    finally:
        storage.close()


def test_attachment_scope_propagates_and_restores_task_identity(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "scope.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    outer = Task(task_id=uuid.uuid4(), task_type="outer")
    outer_token = dexcost.set_current_task(outer)
    attached_id = uuid.uuid4()
    try:
        with tracker.attach_task(attached_id) as attached:
            assert get_current_task() is attached._task
            attached.record_tool_call("remote-tool")
        assert get_current_task() is outer
        assert storage.query_events(task_id=str(attached_id))[0].task_id == attached_id
    finally:
        from dexcost.context import _reset_current_task

        _reset_current_task(outer_token)
        storage.close()


@pytest.mark.asyncio
async def test_attachment_supports_async_scope(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "async-attach.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task_id = uuid.uuid4()
    try:
        async with tracker.attach_task(task_id) as attached:
            assert get_current_task() is attached._task
        assert get_current_task() is None
    finally:
        storage.close()


def test_attachment_rejects_reentry_and_local_identity_mismatch(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "identity.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task_id = uuid.uuid4()
    root_id = uuid.uuid4()
    storage.insert_task(
        Task(
            task_id=task_id,
            task_type="existing",
            root_task_id=root_id,
            parent_task_id=uuid.uuid4(),
        )
    )
    try:
        with pytest.raises(ValueError, match="root_task_id"):
            tracker.attach_task(task_id, root_task_id=uuid.uuid4())

        attached = tracker.attach_task(task_id, root_task_id=root_id)
        with attached, pytest.raises(RuntimeError, match="already active"):
            attached.__enter__()
    finally:
        storage.close()


def test_top_level_attach_task_never_inserts_shadow_task(tmp_path: Path) -> None:
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "global-attach.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    task_id = uuid.uuid4()
    try:
        with dexcost.attach_task(task_id) as attached:
            event = dexcost.report_tool_call("worker-tool")
            assert attached.task_id == event.task_id == task_id
        storage = dexcost._global_tracker._storage  # type: ignore[union-attr]
        assert storage.get_task(str(task_id)) is None
    finally:
        dexcost.close()
