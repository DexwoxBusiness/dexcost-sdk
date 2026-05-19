"""Tests for MCP auto-instrumentation.

All tests use mocked MCP SDK objects -- the real ``mcp`` package is
**not** required.  We simulate the module structure that
:func:`instrument_mcp` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fake MCP module hierarchy
# ---------------------------------------------------------------------------


class _FakeCallToolResult:
    """Simulate an MCP CallToolResult."""

    def __init__(self, *, is_error: bool = False) -> None:
        self.isError = is_error
        self.content = [{"type": "text", "text": "result"}]


class _FakeClientSession:
    """Fake MCP ClientSession with an async call_tool."""

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return _FakeCallToolResult()


def _install_fake_mcp() -> type:
    """Install a fake ``mcp`` package into ``sys.modules``.

    Returns the ClientSession class so tests can customise it.
    """
    mcp_mod = types.ModuleType("mcp")
    client_mod = types.ModuleType("mcp.client")
    session_mod = types.ModuleType("mcp.client.session")

    session_mod.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    client_mod.session = session_mod  # type: ignore[attr-defined]
    mcp_mod.client = client_mod  # type: ignore[attr-defined]

    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.client"] = client_mod
    sys.modules["mcp.client.session"] = session_mod

    return _FakeClientSession


def _uninstall_fake_mcp() -> None:
    """Remove our fake mcp modules from ``sys.modules``."""
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
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
def _fake_mcp() -> Generator[None, None, None]:
    """Install/uninstall fake mcp for every test and ensure uninstrument."""
    _install_fake_mcp()
    yield
    from dexcost.instruments.mcp import uninstrument_mcp

    uninstrument_mcp()
    _uninstall_fake_mcp()


# ---------------------------------------------------------------------------
# Core instrumentation tests
# ---------------------------------------------------------------------------


class TestMCPToolCallRecording:
    """Verify that MCP tool calls are recorded as external_cost events."""

    def test_records_external_cost_event(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """MCP call_tool inside tracked task -> event recorded."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="mcp_test") as task:
            asyncio.run(
                session.call_tool("tavily_search", {"q": "test"})
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "external_cost"
        assert ev.service_name == "mcp:tavily_search"
        assert ev.cost_usd >= Decimal("0")

    def test_details_contain_mcp_fields(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Event details include mcp_tool, mcp_server, latency_ms, is_error."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="details_test") as task:
            asyncio.run(
                session.call_tool("brave_web_search", {"q": "hello"})
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        details = events[0].details
        assert details["mcp_tool"] == "brave_web_search"
        assert "mcp_server" in details
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], int)
        assert details["is_error"] is False

    def test_latency_is_recorded(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Latency is measured and recorded in details."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="latency_test") as task:
            asyncio.run(
                session.call_tool("some_tool")
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms >= 0

    def test_error_tool_call_tracked(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When call_tool raises, the error event is still recorded."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        original_call = ClientSession.call_tool

        async def _failing_call(self: Any, name: str, arguments: Any = None) -> Any:
            raise ConnectionError("MCP server unreachable")

        ClientSession.call_tool = _failing_call  # type: ignore[assignment]

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="error_test") as task:
            with pytest.raises(ConnectionError):
                asyncio.run(
                    session.call_tool("broken_tool")
                )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["is_error"] is True
        assert events[0].service_name == "mcp:broken_tool"

    def test_mcp_result_error_flag(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When MCP result.isError is True, details.is_error reflects it."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        async def _error_result(self: Any, name: str, arguments: Any = None) -> Any:
            return _FakeCallToolResult(is_error=True)

        ClientSession.call_tool = _error_result  # type: ignore[assignment]

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="result_error_test") as task:
            asyncio.run(
                session.call_tool("failing_tool")
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["is_error"] is True


# ---------------------------------------------------------------------------
# Cost resolution tests
# ---------------------------------------------------------------------------


class TestCostResolution:
    """Verify three-tier cost resolution."""

    def test_rate_registry_mcp_prefix(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When a rate is registered for 'mcp:<tool>', that cost is used."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        tracker.register_rate("mcp:tavily_search", per="call", cost_usd="0.008")
        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="rate_test") as task:
            asyncio.run(
                session.call_tool("tavily_search", {"q": "test"})
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.008")
        assert events[0].cost_confidence == "computed"
        assert events[0].pricing_source == "rate_registry"

    def test_service_catalog_mapping_fallback(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When no mcp: rate exists but catalog key is registered, that cost is used."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        # Register rate under the catalog key (not the mcp: prefix)
        tracker.register_rate("brave_search", per="call", cost_usd="0.005")
        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="catalog_test") as task:
            asyncio.run(
                session.call_tool("brave_web_search", {"q": "test"})
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.005")
        assert events[0].cost_confidence == "computed"

    def test_unknown_tool_zero_cost(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Unknown tools get cost=0, confidence='unknown'."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="unknown_test") as task:
            asyncio.run(
                session.call_tool("my_custom_tool")
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0")
        assert events[0].cost_confidence == "unknown"
        assert events[0].pricing_source == "unknown"


# ---------------------------------------------------------------------------
# Instrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """Verify instrument/uninstrument lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        """Calling instrument_mcp twice raises RuntimeError."""
        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_mcp(tracker)

    def test_uninstrument_restores_original(self, tracker: CostTracker) -> None:
        """After uninstrument, call_tool is the original method."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp, uninstrument_mcp

        original = ClientSession.call_tool
        instrument_mcp(tracker)
        assert ClientSession.call_tool is not original

        uninstrument_mcp()
        # After uninstrument, the class attribute is restored
        assert not hasattr(ClientSession.call_tool, "__wrapped__")

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        """Calling uninstrument_mcp when not patched is a safe no-op."""
        from dexcost.instruments.mcp import uninstrument_mcp

        uninstrument_mcp()  # Should not raise


# ---------------------------------------------------------------------------
# Public API tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Verify that MCP instrument functions are exported correctly."""

    def test_instrument_mcp_importable_from_dexcost(self) -> None:
        """instrument_mcp is importable from the top-level dexcost package."""
        from dexcost import instrument_mcp  # noqa: F401

    def test_uninstrument_mcp_importable_from_dexcost(self) -> None:
        """uninstrument_mcp is importable from the top-level dexcost package."""
        from dexcost import uninstrument_mcp  # noqa: F401

    def test_mcp_in_supported_instruments(self) -> None:
        """'mcp' is in ALL_SUPPORTED_INSTRUMENTS."""
        from dexcost.tracker import ALL_SUPPORTED_INSTRUMENTS

        assert "mcp" in ALL_SUPPORTED_INSTRUMENTS
