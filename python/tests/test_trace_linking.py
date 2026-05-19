"""Tests for trace linking (US-033)."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from dexcost.context import get_current_task
from dexcost.integrations.traces import link_trace
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    """Create a fresh SQLite storage for each test."""
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    """Create a CostTracker backed by the tmp-based storage."""
    return CostTracker(storage=storage, auto_instrument=[])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrackedTaskTraceLinks:
    """US-033: TrackedTask.link_trace / get_trace_links."""

    def test_link_trace_stores_in_metadata(
        self,
        tracker: CostTracker,
    ) -> None:
        """TrackedTask.link_trace stores trace in task metadata."""
        with tracker.task(task_type="test") as task:
            task.link_trace("langfuse", "trace-abc-123")

            assert "_trace_links" in task.task.metadata
            links = task.task.metadata["_trace_links"]
            assert len(links) == 1
            assert links[0] == {"provider": "langfuse", "trace_id": "trace-abc-123"}

    def test_link_multiple_traces(
        self,
        tracker: CostTracker,
    ) -> None:
        """Link langfuse + langsmith, both stored."""
        with tracker.task(task_type="test") as task:
            task.link_trace("langfuse", "trace-abc-123")
            task.link_trace("langsmith", "run-def-456")

            links = task.get_trace_links()
            assert len(links) == 2
            assert links[0] == {"provider": "langfuse", "trace_id": "trace-abc-123"}
            assert links[1] == {"provider": "langsmith", "trace_id": "run-def-456"}

    def test_get_trace_links(
        self,
        tracker: CostTracker,
    ) -> None:
        """get_trace_links returns list of linked traces."""
        with tracker.task(task_type="test") as task:
            # Initially empty
            assert task.get_trace_links() == []

            task.link_trace("langfuse", "trace-001")
            result = task.get_trace_links()
            assert len(result) == 1
            assert result[0]["provider"] == "langfuse"
            assert result[0]["trace_id"] == "trace-001"

    def test_link_trace_no_task_context(self) -> None:
        """Standalone helper raises RuntimeError when no task context."""
        # Ensure no task context is active
        assert get_current_task() is None

        with pytest.raises(RuntimeError, match="No active task context"):
            link_trace("langfuse", "trace-xyz")

    def test_trace_survives_serialization(
        self,
        tracker: CostTracker,
    ) -> None:
        """task.to_dict()/from_dict() preserves trace links."""
        with tracker.task(task_type="test") as task:
            task.link_trace("langfuse", "trace-abc-123")
            task.link_trace("langsmith", "run-def-456")

            # Serialize and deserialize
            data = task.task.to_dict()
            restored = Task.from_dict(data)

            assert "_trace_links" in restored.metadata
            links = restored.metadata["_trace_links"]
            assert len(links) == 2
            assert links[0] == {"provider": "langfuse", "trace_id": "trace-abc-123"}
            assert links[1] == {"provider": "langsmith", "trace_id": "run-def-456"}

    def test_link_trace_via_helper(
        self,
        tracker: CostTracker,
    ) -> None:
        """from dexcost.integrations.traces import link_trace works."""
        with tracker.task(task_type="test") as task:
            link_trace("langfuse", "trace-via-helper")

            # Verify via the task's metadata
            links = task.task.metadata.get("_trace_links", [])
            assert len(links) == 1
            assert links[0] == {
                "provider": "langfuse",
                "trace_id": "trace-via-helper",
            }
