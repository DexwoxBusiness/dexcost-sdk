"""General tool tracking across execution shapes and task scopes."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import dexcost
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.models.tool import ToolUsage
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_tool_usage_is_exact_and_rejects_binary_float() -> None:
    assert ToolUsage.from_input("2.500").quantity == Decimal("2.500")
    with pytest.raises(TypeError, match="Decimal, integer, or decimal string"):
        ToolUsage.from_input(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        ToolUsage.from_input(0)
    with pytest.raises(ValueError, match="canonical lowercase"):
        ToolUsage.from_input(1, metric="Page Count")


def test_manual_tool_call_maps_to_strict_v3_without_content(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task_id = uuid.uuid4()
    try:
        with tracker.task(task_type="support.resolve", task_id=task_id) as task:
            event = task.record_tool_call(
                "customer-database",
                operation="lookup",
                duration_ms=125,
                usage=ToolUsage.from_input(3, metric="call_count", unit="Calls"),
                cost_usd="0.015",
                provider="postgresql",
                provider_record_id="query-42",
                dimensions={"cache_hit": True, "rows": 3, "tier": "primary"},
            )

        converted = to_attribution_observation_v3(event, environment="production")
        assert converted is not None
        assert converted["task_id"] == str(task_id)
        assert converted["resource"] == {"type": "tool", "id": "customer-database"}
        assert converted["capability"] == {
            "name": "customer-database",
            "kind": "tool",
            "invocation": "explicit",
        }
        assert converted["operation"]["name"] == "tool.lookup"
        assert converted["operation"]["status"] == "succeeded"
        assert converted["operation"]["latency_ms"] == 125
        assert converted["provider"]["record_id"] == "query-42"
        assert converted["usage"][0]["quantity"] == "3"
        assert [item["key"] for item in converted["usage"][0]["dimensions"]] == [
            "cache_hit",
            "rows",
            "tier",
        ]
        assert "inputs" not in event.details
        assert "output" not in event.details
    finally:
        storage.close()


def test_sync_decorator_records_success_and_preserves_result(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "sync-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    @tracker.track_tool("search", operation="query")
    def search(value: str) -> str:
        return value.upper()

    task_id = uuid.uuid4()
    try:
        with tracker.task(task_type="search.run", task_id=task_id):
            assert search("private prompt") == "PRIVATE PROMPT"
        events = storage.query_events(task_id=str(task_id))
        assert len(events) == 1
        assert events[0].details["attribution_resource_id"] == "search"
        assert "private prompt" not in str(events[0].details)
    finally:
        storage.close()


def test_sync_decorator_records_failure_and_reraises_original(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "failed-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    @tracker.track_tool("payments", operation="authorize")
    def fail() -> None:
        raise LookupError("customer secret")

    try:
        with (
            tracker.task(task_type="payment.run"),
            pytest.raises(LookupError, match="customer secret"),
        ):
            fail()
        event = storage.query_events()[0]
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"] == {"type": "lookuperror"}
        assert "customer secret" not in str(event.details)
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_async_decorator_records_success_and_cancellation(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "async-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    @tracker.track_tool("async-search")
    async def succeed() -> int:
        await asyncio.sleep(0)
        return 7

    @tracker.track_tool("async-cancel")
    async def cancel() -> None:
        raise asyncio.CancelledError

    try:
        with tracker.task(task_type="async.run"):
            assert await succeed() == 7
            with pytest.raises(asyncio.CancelledError):
                await cancel()
        events = storage.query_events()
        by_tool = {event.details["attribution_resource_id"]: event for event in events}
        assert by_tool["async-search"].details["attribution_operation_status"] == "succeeded"
        assert by_tool["async-cancel"].details["attribution_operation_status"] == "cancelled"
    finally:
        storage.close()


def test_generator_is_metered_on_exhaustion_or_early_close(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "generator-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    @tracker.track_tool("pages")
    def pages() -> object:
        yield 1
        yield 2

    try:
        with tracker.task(task_type="generator.run"):
            assert list(pages()) == [1, 2]
            partial = pages()
            assert next(partial) == 1
            partial.close()  # type: ignore[attr-defined]
        statuses = [
            event.details["attribution_operation_status"]
            for event in reversed(storage.query_events())
        ]
        assert statuses == ["succeeded", "cancelled"]
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_async_generator_is_metered_on_early_close(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "async-generator-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    @tracker.track_tool("async-pages")
    async def pages() -> object:
        yield 1
        yield 2

    try:
        with tracker.task(task_type="async-generator.run"):
            stream = pages()
            assert await stream.__anext__() == 1  # type: ignore[attr-defined]
            await stream.aclose()  # type: ignore[attr-defined]
        event = storage.query_events()[0]
        assert event.details["attribution_operation_status"] == "cancelled"
    finally:
        storage.close()


def test_top_level_decorator_can_be_declared_before_init_and_creates_auto_task(
    tmp_path: Path,
) -> None:
    @dexcost.track_tool("document-parser", operation="parse")
    def parse() -> int:
        return 42

    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "top-level.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    try:
        assert parse() == 42
        storage = dexcost._global_tracker._storage  # type: ignore[union-attr]
        tasks = storage.query_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_type == "tool.document-parser"
        assert tasks[0].status == "success"
        assert len(storage.query_events(task_id=str(tasks[0].task_id))) == 1
    finally:
        dexcost.close()


def test_top_level_manual_reporting_accepts_cross_process_task_id(tmp_path: Path) -> None:
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "attached.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    task_id = uuid.uuid4()
    try:
        event = dexcost.report_tool_call("remote-worker", task_id=task_id)
        assert event.task_id == task_id
        storage = dexcost._global_tracker._storage  # type: ignore[union-attr]
        assert storage.get_task(str(task_id)) is None
        assert storage.query_events(task_id=str(task_id)) == [event]
    finally:
        dexcost.close()


def test_explicit_retry_correlation_is_preserved(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "retry-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    operation_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    try:
        with tracker.task(task_type="retry.run") as task:
            first = task.record_tool_call(
                "browser",
                operation_id=operation_id,
                attempt_id=first_id,
            )
            second = task.record_tool_call(
                "browser",
                operation_id=operation_id,
                attempt_id=second_id,
                attempt_number=2,
                retry_of=first_id,
            )
        assert first.event_id == first_id
        converted = to_attribution_observation_v3(second)
        assert converted is not None
        assert converted["operation"]["id"] == str(operation_id)
        assert converted["operation"]["attempt"] == {
            "id": str(second_id),
            "number": 2,
            "retry_of": str(first_id),
        }
    finally:
        storage.close()
