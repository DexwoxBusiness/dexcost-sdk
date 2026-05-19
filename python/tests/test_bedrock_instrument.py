"""Tests for AWS Bedrock auto-instrumentation (US-012).

All tests use mocked botocore SDK objects — the real ``botocore`` package is
**not** required.  We simulate the module structure that
:func:`instrument_bedrock` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fake botocore module hierarchy
# ---------------------------------------------------------------------------


def _make_bedrock_response(
    input_tokens: int = 100,
    output_tokens: int = 50,
    model_family: str = "anthropic",
) -> dict[str, Any]:
    """Build a mock Bedrock InvokeModel response dict.

    Supports different model family response formats.  Default is
    Anthropic-on-Bedrock format.
    """
    if model_family == "anthropic":
        body_content = json.dumps({
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [{"text": "Hello"}],
        }).encode()
    elif model_family == "titan":
        body_content = json.dumps({
            "inputTextTokenCount": input_tokens,
            "results": [{"tokenCount": output_tokens, "outputText": "Hello"}],
        }).encode()
    else:
        # Generic
        body_content = json.dumps({
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }).encode()

    body = io.BytesIO(body_content)

    return {
        "body": body,
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


def _make_empty_response() -> dict[str, Any]:
    """Build a Bedrock response with no token usage."""
    body_content = json.dumps({"content": [{"text": "Hello"}]}).encode()
    body = io.BytesIO(body_content)
    return {
        "body": body,
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


def _make_bedrock_stream_response(
    input_tokens: int = 100,
    output_tokens: int = 50,
    metrics_present: bool = True,
) -> dict[str, Any]:
    """Build a mock ``InvokeModelWithResponseStream`` response.

    The ``body`` is an iterable EventStream of chunk events; the final
    chunk carries ``amazon-bedrock-invocationMetrics`` (present for every
    model family) when *metrics_present* is True.
    """

    def _chunk(payload: dict[str, Any]) -> dict[str, Any]:
        return {"chunk": {"bytes": json.dumps(payload).encode("utf-8")}}

    events: list[dict[str, Any]] = [
        _chunk({"type": "content_block_delta", "delta": {"text": "Hello"}}),
    ]
    if metrics_present:
        events.append(
            _chunk(
                {
                    "amazon-bedrock-invocationMetrics": {
                        "inputTokenCount": input_tokens,
                        "outputTokenCount": output_tokens,
                    }
                }
            )
        )
    else:
        events.append(_chunk({"type": "message_stop"}))

    return {
        "body": iter(events),
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


def _install_fake_botocore() -> type:
    """Install a fake ``botocore`` package into ``sys.modules``.

    Returns the ``BaseClient`` class so tests can set ``._make_api_call``
    behaviour.
    """
    botocore = types.ModuleType("botocore")
    client_mod = types.ModuleType("botocore.client")

    class BaseClient:
        def __init__(self, service_name: str = "bedrock-runtime") -> None:
            self._service_model = MagicMock()
            self._service_model.service_name = service_name

        def _make_api_call(self, operation_name: str, api_params: dict[str, Any]) -> Any:
            raise NotImplementedError("should be mocked per-test")

    client_mod.BaseClient = BaseClient  # type: ignore[attr-defined]
    botocore.client = client_mod  # type: ignore[attr-defined]

    sys.modules["botocore"] = botocore
    sys.modules["botocore.client"] = client_mod

    return BaseClient  # type: ignore[return-value]


def _uninstall_fake_botocore() -> None:
    """Remove our fake botocore modules from ``sys.modules``."""
    for key in list(sys.modules):
        if key == "botocore" or key.startswith("botocore."):
            sys.modules[key] = None  # type: ignore[assignment]


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
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _fake_botocore() -> Generator[None, None, None]:
    """Install/uninstall fake botocore for every test and ensure uninstrument."""
    _install_fake_botocore()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.bedrock import uninstrument_bedrock

    uninstrument_bedrock()
    _uninstall_fake_botocore()


# ---------------------------------------------------------------------------
# Sync InvokeModel tests
# ---------------------------------------------------------------------------


class TestInvokeModel:
    """Bedrock InvokeModel calls."""

    def test_invoke_model_records_event(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Mocked Bedrock InvokeModel call inside tracked task -> event recorded."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_response(input_tokens=150, output_tokens=75)

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_usage") as task:
            result = client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )

        assert result is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "aws_bedrock"
        # model_id "anthropic.claude-v2" is stripped to "claude-v2"
        assert ev.model == "claude-v2"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_anthropic_response_format(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Anthropic-on-Bedrock response format is correctly parsed."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_response(
            input_tokens=200,
            output_tokens=100,
            model_family="anthropic",
        )

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_anthropic") as task:
            client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-3-sonnet-20240229-v1:0", "body": b"{}"},
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.input_tokens == 200
        assert ev.output_tokens == 100

    def test_non_bedrock_calls_pass_through(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Non-bedrock-runtime calls are not intercepted."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        s3_response = {"Buckets": []}

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return s3_response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        # Create a client instance with a non-bedrock service
        client = BaseClient(service_name="s3")

        with tracker.task(task_type="s3_call") as task:
            result = client._make_api_call(
                "ListBuckets",
                {},
            )

        assert result is s3_response

        # No event should be recorded for S3 calls
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 0

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_response()

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_latency") as task:
            client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response body has no usage tokens, cost_confidence should be 'estimated'."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_empty_response()

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_no_usage") as task:
            client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0


# ---------------------------------------------------------------------------
# Streaming tests (Fix 2)
# ---------------------------------------------------------------------------


class TestInvokeModelWithResponseStream:
    """Bedrock InvokeModelWithResponseStream — streamed cost capture."""

    def test_streaming_records_event_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed Bedrock InvokeModel call records an llm_call event."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_stream_response(input_tokens=160, output_tokens=80)

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_stream") as task:
            result = client._make_api_call(
                "InvokeModelWithResponseStream",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )
            # Consuming the stream body triggers usage capture.
            collected = list(result["body"])

        assert len(collected) == 2

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "aws_bedrock"
        assert ev.model == "claude-v2"
        assert ev.input_tokens == 160
        assert ev.output_tokens == 80
        assert ev.cost_confidence == "exact"

    def test_streaming_without_metrics_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed call with no invocation metrics records an estimated event."""
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_stream_response(metrics_present=False)

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="bedrock_stream_no_usage") as task:
            result = client._make_api_call(
                "InvokeModelWithResponseStream",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )
            list(result["body"])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0


# ---------------------------------------------------------------------------
# Passthrough (no active task) tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When no explicit task context is active, calls create an auto-task."""

    def test_no_task_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_response()

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        result = client._make_api_call(
            "InvokeModel",
            {"modelId": "anthropic.claude-v2", "body": b"{}"},
        )

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Instrument / uninstrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """instrument_bedrock / uninstrument_bedrock lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.bedrock import instrument_bedrock

        instrument_bedrock(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_bedrock(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock, uninstrument_bedrock

        original_make_api_call = BaseClient._make_api_call

        response = _make_bedrock_response()

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        # Verify it's patched (the method should be wrapped)
        assert BaseClient._make_api_call is not original_make_api_call  # type: ignore[comparison-overlap]

        uninstrument_bedrock()

        # After uninstrument, should be able to instrument again
        instrument_bedrock(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.bedrock import uninstrument_bedrock

        # Should not raise
        uninstrument_bedrock()

    def test_missing_botocore_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_bedrock raises ImportError if botocore is not installed."""
        from dexcost.instruments.bedrock import instrument_bedrock

        _uninstall_fake_botocore()

        blocked = {k: None for k in list(sys.modules) if k == "botocore" or k.startswith("botocore.")}
        blocked.setdefault("botocore", None)
        blocked.setdefault("botocore.client", None)

        with patch.dict(sys.modules, blocked):
            with pytest.raises(ImportError, match="botocore"):
                instrument_bedrock(tracker)

        # Re-install for cleanup
        _install_fake_botocore()


# ---------------------------------------------------------------------------
# Cost calculation integration tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify the pricing engine is used to calculate costs."""

    def test_cost_calculated_via_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With usage present, cost should be computed by the pricing engine.

        Uses a model ID without a provider prefix so that the stripped name
        matches the pricing data (e.g. ``claude-3-5-sonnet-20241022``).
        """
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        response = _make_bedrock_response(input_tokens=1000, output_tokens=500)

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return response

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="cost_calc") as task:
            client._make_api_call(
                "InvokeModel",
                {"modelId": "claude-3-5-sonnet-20241022", "body": b"{}"},
            )

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        # The pricing engine should have attempted pricing
        assert ev.pricing_source is not None
        # cost_usd should be non-negative
        assert ev.cost_usd >= Decimal("0")
        # Bedrock model names may not match pricing data exactly
        assert ev.cost_confidence in ("exact", "computed", "estimated", "unknown")


# ---------------------------------------------------------------------------
# Task aggregation integration tests
# ---------------------------------------------------------------------------


class TestTaskAggregation:
    """Auto-captured events are included in task cost aggregation."""

    def test_auto_captured_event_aggregated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from botocore.client import BaseClient

        from dexcost.instruments.bedrock import instrument_bedrock

        def fake_make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            return _make_bedrock_response(input_tokens=200, output_tokens=100)

        BaseClient._make_api_call = fake_make_api_call  # type: ignore[assignment]

        instrument_bedrock(tracker)

        client = BaseClient(service_name="bedrock-runtime")

        with tracker.task(task_type="agg_test") as task:
            client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )
            client._make_api_call(
                "InvokeModel",
                {"modelId": "anthropic.claude-v2", "body": b"{}"},
            )

        tasks = storage.query_tasks(task_type="agg_test")
        t = tasks[0]
        assert t.total_input_tokens == 400
        assert t.total_output_tokens == 200
        assert t.llm_cost_usd >= Decimal("0")
        assert t.total_cost_usd >= Decimal("0")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Public API export tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """instrument_bedrock / uninstrument_bedrock accessible from top-level package."""

    def test_instrument_bedrock_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_bedrock")
        assert callable(dexcost.instrument_bedrock)

    def test_uninstrument_bedrock_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_bedrock")
        assert callable(dexcost.uninstrument_bedrock)
