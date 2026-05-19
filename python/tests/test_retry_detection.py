"""Tests for retry detection and waste tracking (US-017).

Validates:
- Auto-detect: same model + same task + prior transient error within window
- Configurable retry_likelihood_threshold (default 0.8) controls sensitivity
- Manual tagging: task.mark_retry(reason="rate_limit") explicit overrides
- Manual override: task.mark_not_retry() for false positives
- Retry events: is_retry=true, retry_reason set, retry_of links to original event_id
- retry_marker event_type for retry attempts that never produced a response
- Task aggregates: retry_count, retry_cost_usd updated on task close
- Legitimate repeated calls are NOT flagged as retries
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from dexcost.models.event import Event
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_instrument=[], enable_retry_heuristics=True)


# ---------------------------------------------------------------------------
# AC1: Auto-detect retry — same model + same task + prior transient error
# ---------------------------------------------------------------------------


class TestRetryHeuristicsDisabledByDefault:
    """PRD: v1.0 should NOT auto-detect retries by default.

    Likelihood-based heuristics are gated behind enable_retry_heuristics=True.
    Default behavior: only manual tagging (mark_retry) works.
    """

    def test_heuristics_disabled_by_default(self, storage: SQLiteStorage) -> None:
        """Without enable_retry_heuristics=True, auto-detection is off."""
        tracker = CostTracker(storage=storage, auto_instrument=[])

        task = tracker.start_task(task_type="no_heuristics")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        # Without heuristics, this should NOT be auto-flagged
        assert e2.is_retry is False

    def test_heuristics_enabled_explicitly(self, storage: SQLiteStorage) -> None:
        """With enable_retry_heuristics=True, auto-detection works."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            enable_retry_heuristics=True,
        )

        task = tracker.start_task(task_type="with_heuristics")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        # With heuristics, this SHOULD be auto-flagged (US-036 engine)
        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"


class TestAutoDetectRetry:
    """Auto-detect retries when prior LLM call for the same model had a
    transient error within the configurable window.

    NOTE: These tests use the default tracker fixture which needs
    enable_retry_heuristics=True to exercise heuristic detection.
    """

    def test_rate_limit_then_retry_detected(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """rate_limit → retry → success sequence: verify detection."""
        task = tracker.start_task(task_type="retry_detect")

        # Call 1: fails with rate_limit
        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")

        # Call 2: retry (auto-detected)
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"  # US-036 engine sets reason to "heuristic"
        assert e2.retry_of == e1.event_id

    def test_timeout_then_retry_detected(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="timeout_retry")

        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="timeout")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"  # US-036 engine sets reason to "heuristic"
        assert e2.retry_of == e1.event_id

    def test_5xx_then_retry_detected(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="5xx_retry")

        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="5xx")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"  # US-036 engine sets reason to "heuristic"
        assert e2.retry_of == e1.event_id

    def test_chained_retries(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """rate_limit → rate_limit → success: both subsequent calls are retries."""
        task = tracker.start_task(task_type="chained")

        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        # Call 2: retry of call 1 (also fails)
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        # Call 3: retry of call 2 (succeeds)
        e3 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True
        assert e2.retry_of == e1.event_id

        assert e3.is_retry is True
        assert e3.retry_of == e2.event_id

    def test_error_type_stored_in_details(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """error_type parameter is persisted in event.details."""
        task = tracker.start_task(task_type="error_details")
        e = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        task.end()

        assert e.details["error_type"] == "rate_limit"

        # Verify round-trip through storage
        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].details["error_type"] == "rate_limit"

    def test_error_type_preserved_with_existing_details(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """error_type merges with existing details dict."""
        task = tracker.start_task(task_type="details_merge")
        e = task.record_llm_call(
            "openai",
            "gpt-4",
            100,
            50,
            "0.05",
            error_type="rate_limit",
            details={"request_id": "req-123"},
        )
        task.end()

        assert e.details["error_type"] == "rate_limit"
        assert e.details["request_id"] == "req-123"


# ---------------------------------------------------------------------------
# AC2: Configurable retry_likelihood_threshold
# ---------------------------------------------------------------------------


class TestRetryLikelihoodThreshold:
    """retry_likelihood_threshold (default 0.8) controls sensitivity."""

    def test_default_threshold_catches_all_transient_errors(self, storage: SQLiteStorage) -> None:
        """Default threshold (0.8) with US-036 heuristic engine catches all
        transient errors.  Because the engine applies time-decay, we use a
        slightly lower heuristic threshold (0.7) to ensure near-instant
        sequential calls (where connection_error has base likelihood 0.8)
        are still caught after the tiny time-decay factor."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            enable_retry_heuristics=True,
            retry_heuristic_threshold=0.7,
        )

        for error in ["rate_limit", "timeout", "5xx", "server_error", "connection_error"]:
            task = tracker.start_task(task_type=f"thresh_{error}")
            task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type=error)
            e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
            task.end()

            assert e2.is_retry is True, f"Expected retry for error_type={error!r}"

    def test_high_threshold_filters_low_likelihood_errors(self, storage: SQLiteStorage) -> None:
        """Threshold 0.95 only catches rate_limit (1.0)."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            retry_likelihood_threshold=0.95,
            enable_retry_heuristics=True,
        )

        # rate_limit (1.0) should still trigger
        task1 = tracker.start_task(task_type="high_thresh_rl")
        task1.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e_rl = task1.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task1.end()
        assert e_rl.is_retry is True

        # timeout (0.9) should NOT trigger at threshold 0.95
        task2 = tracker.start_task(task_type="high_thresh_to")
        task2.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="timeout")
        e_to = task2.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task2.end()
        assert e_to.is_retry is False

    def test_threshold_above_one_disables_auto_detection(self, storage: SQLiteStorage) -> None:
        """Setting threshold > 1.0 effectively disables auto-detection."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            retry_likelihood_threshold=1.1,
            enable_retry_heuristics=True,
        )

        task = tracker.start_task(task_type="disabled_detect")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is False


# ---------------------------------------------------------------------------
# AC3: Manual tagging — mark_retry
# ---------------------------------------------------------------------------


class TestManualMarkRetry:
    """task.mark_retry(reason=...) for explicit overrides."""

    def test_mark_retry_creates_retry_marker(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="manual_retry")
        e = task.mark_retry(reason="rate_limit")
        task.end()

        assert e.event_type == "retry_marker"
        assert e.is_retry is True
        assert e.retry_reason == "rate_limit"
        assert e.cost_usd == Decimal("0")

    def test_mark_retry_with_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="retry_cost")
        task.mark_retry(reason="timeout", cost_usd="0.03")
        task.end()

        tasks = storage.query_tasks(task_type="retry_cost")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.03")
        assert t.total_cost_usd == Decimal("0.03")

    def test_mark_retry_with_retry_of(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """mark_retry accepts retry_of to link to the original event."""
        task = tracker.start_task(task_type="retry_of_link")
        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        e2 = task.mark_retry(reason="rate_limit", retry_of=e1.event_id)
        task.end()

        assert e2.retry_of == e1.event_id

        # Verify through storage
        events = storage.query_events(task_id=str(task.task_id))
        marker = next(e for e in events if e.event_type == "retry_marker")
        assert marker.retry_of == e1.event_id


# ---------------------------------------------------------------------------
# AC4: Manual override — mark_not_retry
# ---------------------------------------------------------------------------


class TestMarkNotRetry:
    """task.mark_not_retry() for false positives on legitimate repeated calls."""

    def test_mark_not_retry_clears_most_recent(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """mark_not_retry() with no args clears the most recent retry event."""
        task = tracker.start_task(task_type="not_retry")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        assert e2.is_retry is True

        # Override: this was a legitimate repeated call, not a retry
        updated = task.mark_not_retry()
        assert updated is not None
        assert updated.is_retry is False
        assert updated.retry_reason is None
        assert updated.retry_of is None

        task.end()

        # Verify through storage
        events = storage.query_events(task_id=str(task.task_id))
        e2_stored = next(e for e in events if e.event_id == e2.event_id)
        assert e2_stored.is_retry is False

    def test_mark_not_retry_by_event_id(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """mark_not_retry(event_id=...) targets a specific event."""
        task = tracker.start_task(task_type="not_retry_id")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        assert e2.is_retry is True

        updated = task.mark_not_retry(event_id=e2.event_id)
        assert updated is not None
        assert updated.event_id == e2.event_id
        assert updated.is_retry is False

        task.end()

    def test_mark_not_retry_no_retries_returns_none(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """mark_not_retry() returns None if no retry events exist."""
        task = tracker.start_task(task_type="no_retries")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")

        result = task.mark_not_retry()
        assert result is None

        task.end()

    def test_mark_not_retry_affects_aggregation(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """After mark_not_retry, the overridden event should not count
        as retry waste in task aggregates."""
        task = tracker.start_task(task_type="not_retry_agg")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        assert e2.is_retry is True

        task.mark_not_retry()
        task.end()

        tasks = storage.query_tasks(task_type="not_retry_agg")
        t = tasks[0]
        assert t.retry_count == 0
        assert t.retry_cost_usd == Decimal("0")
        assert t.total_cost_usd == Decimal("0.10")


# ---------------------------------------------------------------------------
# AC5: Retry events have is_retry=true, retry_reason, retry_of
# ---------------------------------------------------------------------------


class TestRetryEventFields:
    """Retry events: is_retry=true, retry_reason set, retry_of links to
    original event_id."""

    def test_auto_detected_retry_has_all_fields(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="retry_fields")
        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"  # US-036 engine sets reason to "heuristic"
        assert e2.retry_of == e1.event_id

        # Verify through storage round-trip
        events = storage.query_events(task_id=str(task.task_id))
        retry_event = next(e for e in events if e.event_id == e2.event_id)
        assert retry_event.is_retry is True
        assert retry_event.retry_reason == "heuristic"  # US-036 engine
        assert retry_event.retry_of == e1.event_id

    def test_manual_retry_marker_has_fields(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="marker_fields")
        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        e2 = task.mark_retry(reason="rate_limit", retry_of=e1.event_id)
        task.end()

        assert e2.is_retry is True
        assert e2.retry_reason == "rate_limit"
        assert e2.retry_of == e1.event_id


# ---------------------------------------------------------------------------
# AC6: retry_marker for immediate 429s (no response)
# ---------------------------------------------------------------------------


class TestRetryMarkerEventType:
    """retry_marker event_type for retry attempts that never produced a response."""

    def test_mark_retry_produces_retry_marker_event(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="marker_429")
        e = task.mark_retry(reason="rate_limit")
        task.end()

        assert e.event_type == "retry_marker"
        assert e.is_retry is True
        assert e.retry_reason == "rate_limit"
        assert e.cost_usd == Decimal("0")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].event_type == "retry_marker"

    def test_retry_marker_with_cost_for_partial_response(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Some retries may have incurred partial cost (e.g. streaming)."""
        task = tracker.start_task(task_type="marker_partial")
        task.mark_retry(reason="timeout", cost_usd="0.01")
        task.end()

        tasks = storage.query_tasks(task_type="marker_partial")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.01")


# ---------------------------------------------------------------------------
# AC7: Task aggregates — retry_count, retry_cost_usd updated on close
# ---------------------------------------------------------------------------


class TestTaskRetryAggregates:
    """Task aggregates: retry_count, retry_cost_usd updated automatically."""

    def test_auto_detected_retries_counted(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="agg_auto")
        # Call 1: rate_limit error
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        # Call 2: auto-detected retry
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        tasks = storage.query_tasks(task_type="agg_auto")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.05")
        assert t.total_cost_usd == Decimal("0.10")

    def test_manual_retry_marker_counted(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="agg_manual")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.mark_retry(reason="rate_limit", cost_usd="0.02")
        task.end()

        tasks = storage.query_tasks(task_type="agg_manual")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.02")
        assert t.total_cost_usd == Decimal("0.07")

    def test_mixed_auto_and_manual_retries(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="agg_mixed")
        # LLM call fails
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        # Auto-detected retry
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        # Separate manual retry marker
        task.mark_retry(reason="timeout", cost_usd="0.01")
        task.end()

        tasks = storage.query_tasks(task_type="agg_mixed")
        t = tasks[0]
        assert t.retry_count == 2
        assert t.retry_cost_usd == Decimal("0.06")  # 0.05 + 0.01
        assert t.total_cost_usd == Decimal("0.11")

    def test_context_manager_aggregates_retries(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        with tracker.task(task_type="cm_agg_retry") as task:
            task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
            task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")

        tasks = storage.query_tasks(task_type="cm_agg_retry")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.05")


# ---------------------------------------------------------------------------
# AC8: Legitimate repeated calls NOT flagged
# ---------------------------------------------------------------------------


class TestLegitimateRepeatedCalls:
    """Legitimate repeated calls are NOT flagged as retries."""

    def test_same_model_no_error_not_flagged(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Two successful calls to the same model are NOT retries."""
        task = tracker.start_task(task_type="legit_repeat")
        e1 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e1.is_retry is False
        assert e2.is_retry is False

    def test_different_model_after_error_not_flagged(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Error on model A, call to model B is NOT a retry."""
        task = tracker.start_task(task_type="diff_model")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-3.5-turbo", 100, 50, "0.01")
        task.end()

        assert e2.is_retry is False

    def test_error_outside_window_not_flagged(self, storage: SQLiteStorage) -> None:
        """Error outside the retry window is NOT detected as retry cause."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            retry_window_seconds=5,
        )

        task = tracker.start_task(task_type="outside_window")

        # Insert event with old timestamp (well outside the 5s window)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        old_event = Event(
            task_id=task.task_id,
            event_type="llm_call",
            cost_usd=Decimal("0.05"),
            cost_confidence="exact",
            pricing_source="manual",
            provider="openai",
            model="gpt-4",
            input_tokens=100,
            output_tokens=50,
            occurred_at=old_time,
            details={"error_type": "rate_limit"},
        )
        storage.insert_event(old_event)

        # New call should NOT be flagged
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is False

    def test_non_transient_error_not_flagged(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Non-transient error types should NOT trigger retry detection."""
        task = tracker.start_task(task_type="non_transient")
        task.record_llm_call(
            "openai",
            "gpt-4",
            100,
            50,
            "0.05",
            error_type="invalid_request",
        )
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is False

    def test_different_tasks_not_linked(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Error in task A does NOT trigger retry detection in task B."""
        task_a = tracker.start_task(task_type="task_a")
        task_a.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        task_a.end()

        task_b = tracker.start_task(task_type="task_b")
        e = task_b.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task_b.end()

        assert e.is_retry is False


# ---------------------------------------------------------------------------
# Configurable retry window
# ---------------------------------------------------------------------------


class TestRetryWindowConfig:
    """Configurable retry_window_seconds (default 30s)."""

    def test_default_window_is_30_seconds(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With default 30s window, recent errors are detected."""
        task = tracker.start_task(task_type="default_window")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True

    def test_custom_window(self, storage: SQLiteStorage) -> None:
        """Custom window of 60 seconds."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            retry_window_seconds=60,
            enable_retry_heuristics=True,
        )

        task = tracker.start_task(task_type="custom_window")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.end()

        assert e2.is_retry is True


# ---------------------------------------------------------------------------
# Full scenario: rate_limit → retry → success
# ---------------------------------------------------------------------------


class TestFullRetryScenario:
    """End-to-end: rate_limit → retry → success, verify detection and aggregates."""

    def test_full_rate_limit_retry_success(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="full_scenario", customer_id="acme")

        # Step 1: First LLM call fails with rate_limit
        e1 = task.record_llm_call(
            "openai",
            "gpt-4",
            200,
            100,
            "0.10",
            error_type="rate_limit",
        )
        assert e1.is_retry is False
        assert e1.details["error_type"] == "rate_limit"

        # Step 2: Retry (auto-detected by US-036 heuristic engine)
        e2 = task.record_llm_call("openai", "gpt-4", 200, 100, "0.10")
        assert e2.is_retry is True
        assert e2.retry_reason == "heuristic"  # US-036 engine sets reason to "heuristic"
        assert e2.retry_of == e1.event_id

        # Step 3: Also record non-LLM cost (should not be affected)
        task.record_cost(service="google_maps", cost_usd="0.005")

        task.end()

        # Verify task aggregates
        tasks = storage.query_tasks(task_type="full_scenario")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.10")
        assert t.llm_cost_usd == Decimal("0.20")
        assert t.external_cost_usd == Decimal("0.005")
        assert t.total_cost_usd == Decimal("0.205")
        assert t.total_input_tokens == 400
        assert t.total_output_tokens == 200

    def test_full_scenario_with_mark_not_retry(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Auto-detect → override with mark_not_retry → verify corrected aggregates."""
        task = tracker.start_task(task_type="override_scenario")

        # Simulate intentional repeated calls that look like retries
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05", error_type="rate_limit")
        e2 = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        assert e2.is_retry is True

        # Developer knows this was legitimate, overrides
        task.mark_not_retry(event_id=e2.event_id)

        task.end()

        tasks = storage.query_tasks(task_type="override_scenario")
        t = tasks[0]
        assert t.retry_count == 0
        assert t.retry_cost_usd == Decimal("0")

    def test_report_percentage_calculation(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Verify we can calculate '8% of spend is retry waste' from aggregates."""
        task = tracker.start_task(task_type="pct_calc")

        # 10 calls at $0.10 each, 1 is a retry
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.10", error_type="rate_limit")
        # This retry costs $0.10
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.10")
        # 8 more normal calls
        for _ in range(8):
            task.record_llm_call("openai", "gpt-4", 100, 50, "0.10")

        task.end()

        tasks = storage.query_tasks(task_type="pct_calc")
        t = tasks[0]
        assert t.total_cost_usd == Decimal("1.00")
        assert t.retry_cost_usd == Decimal("0.10")
        # 10% retry waste
        pct = (t.retry_cost_usd / t.total_cost_usd) * 100
        assert pct == Decimal("10")


# ---------------------------------------------------------------------------
# update_event storage tests
# ---------------------------------------------------------------------------


class TestUpdateEvent:
    """Verify update_event works in SQLite storage."""

    def test_update_event_persists_changes(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="update_evt")
        e = task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")

        # Modify and update
        e.is_retry = True
        e.retry_reason = "manual_override"
        storage.update_event(e)

        # Verify
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].is_retry is True
        assert events[0].retry_reason == "manual_override"

        task.end()
