"""Tests for adaptive batch splitting in the SyncWorker.

Verifies that oversized batches are automatically split before pushing
to the server, preventing SQS 256KB payload limit issues.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

import pytest

from dexcost.config import DexcostConfig
from dexcost.models.event import Event
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker, _MAX_PAYLOAD_BYTES


def _make_config(**overrides: Any) -> DexcostConfig:
    defaults: dict[str, Any] = dict(
        api_key="dx_live_test123",
        batch_size=100,
        flush_interval_seconds=60.0,
    )
    defaults.update(overrides)
    return DexcostConfig(**defaults)


def _make_event(details: dict[str, Any] | None = None) -> Event:
    return Event(
        task_id=uuid.uuid4(),
        event_type="llm_call",
        provider="openai",
        model="gpt-4",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.05"),
        cost_confidence="exact",
        details=details or {},
    )


def _make_large_event(size_bytes: int = 50000) -> Event:
    """Create an event with a large details dict."""
    padding = "x" * size_bytes
    return _make_event(details={"large_field": padding})


class TestBatchSplitting:
    """Adaptive batch splitting tests."""

    def test_small_batch_sends_single_request(self, tmp_path: Path) -> None:
        """A batch under the size limit sends one request."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        events = [_make_event().to_dict() for _ in range(5)]
        tasks: list[dict[str, Any]] = []

        with patch.object(worker, "_post_raw") as mock_post:
            worker._post_with_split(events, tasks)
            assert mock_post.call_count == 1

    def test_large_batch_splits_into_multiple_requests(self, tmp_path: Path) -> None:
        """A batch over the size limit is split into multiple requests."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        # Create events that together exceed MAX_PAYLOAD_BYTES
        # 10 events * ~30KB each = ~300KB > 200KB limit
        events = [_make_large_event(size_bytes=30000).to_dict() for _ in range(10)]
        tasks: list[dict[str, Any]] = []

        with patch.object(worker, "_post_raw") as mock_post:
            worker._post_with_split(events, tasks)
            assert mock_post.call_count >= 2, (
                f"Expected >=2 calls, got {mock_post.call_count}"
            )

    def test_single_oversized_event_is_skipped(self, tmp_path: Path) -> None:
        """A single event exceeding the limit is skipped, not retried forever."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        # Single event larger than MAX_PAYLOAD_BYTES
        huge_event = _make_large_event(size_bytes=250000).to_dict()

        with patch.object(worker, "_post_raw") as mock_post:
            # Should not raise, should not call _post_raw (event skipped)
            worker._post_with_split([huge_event], [])
            # Skipped: 0 calls because the single event is too large
            assert mock_post.call_count == 0

    def test_tasks_only_sent_with_first_chunk(self, tmp_path: Path) -> None:
        """Tasks are sent with the first chunk only, not duplicated."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        events = [_make_large_event(size_bytes=30000).to_dict() for _ in range(10)]
        tasks = [{"task_id": str(uuid.uuid4()), "task_type": "test"}]

        payloads_sent: list[dict[str, Any]] = []

        def capture_post(body: bytes) -> None:
            payloads_sent.append(json.loads(body.decode("utf-8")))

        with patch.object(worker, "_post_raw", side_effect=capture_post):
            worker._post_with_split(events, tasks)

        # First payload should have tasks, subsequent should have empty tasks
        assert len(payloads_sent) >= 2
        assert len(payloads_sent[0].get("tasks", [])) > 0
        for payload in payloads_sent[1:]:
            assert len(payload.get("tasks", [])) == 0

    def test_recursive_split_handles_very_large_batch(self, tmp_path: Path) -> None:
        """A very large batch splits recursively multiple times."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        # 50 events * ~20KB each = ~1MB — needs multiple splits
        events = [_make_large_event(size_bytes=20000).to_dict() for _ in range(50)]

        with patch.object(worker, "_post_raw") as mock_post:
            worker._post_with_split(events, [])
            # Should split multiple times — at least 4-5 chunks
            assert mock_post.call_count >= 4

    def test_max_payload_bytes_constant_exists(self) -> None:
        """The MAX_PAYLOAD_BYTES constant is set correctly."""
        assert _MAX_PAYLOAD_BYTES == 200_000
        assert _MAX_PAYLOAD_BYTES < 256_000  # Must be under SQS limit

    def test_all_events_included_across_chunks(self, tmp_path: Path) -> None:
        """Every event from the original batch appears in exactly one chunk."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        events = [_make_large_event(size_bytes=30000).to_dict() for _ in range(10)]
        original_ids = {e["event_id"] for e in events}

        sent_ids: set[str] = set()

        def capture_post(body: bytes) -> None:
            payload = json.loads(body.decode("utf-8"))
            for ev in payload["events"]:
                sent_ids.add(ev["event_id"])

        with patch.object(worker, "_post_raw", side_effect=capture_post):
            worker._post_with_split(events, [])

        assert sent_ids == original_ids, "Not all events were sent after splitting"

    def test_depth_limit_prevents_infinite_recursion(self, tmp_path: Path) -> None:
        """Splitting stops at max depth even if payload is still too large."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        # 2 events each > MAX_PAYLOAD_BYTES individually, but with 2 events
        # it will split to single events then hit the single-event skip path.
        # Use depth parameter directly to test the depth guard.
        events = [_make_large_event(size_bytes=100000).to_dict() for _ in range(4)]

        with patch.object(worker, "_post_raw") as mock_post:
            # Call at depth 5 (the max) — should post raw regardless of size
            worker._post_with_split(events, [], depth=5)
            assert mock_post.call_count == 1

    def test_empty_batch_sends_single_request(self, tmp_path: Path) -> None:
        """An empty events list still sends one request (with tasks)."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        tasks = [{"task_id": str(uuid.uuid4()), "task_type": "test"}]

        with patch.object(worker, "_post_raw") as mock_post:
            worker._post_with_split([], tasks)
            assert mock_post.call_count == 1

    def test_each_chunk_is_valid_json(self, tmp_path: Path) -> None:
        """Every chunk POSTed is valid JSON with events and tasks keys."""
        config = _make_config()
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        worker = SyncWorker(config=config, storage=storage)

        events = [_make_large_event(size_bytes=30000).to_dict() for _ in range(10)]

        def validate_post(body: bytes) -> None:
            payload = json.loads(body.decode("utf-8"))
            assert "events" in payload
            assert "tasks" in payload
            assert isinstance(payload["events"], list)
            assert isinstance(payload["tasks"], list)

        with patch.object(worker, "_post_raw", side_effect=validate_post):
            worker._post_with_split(events, [])
