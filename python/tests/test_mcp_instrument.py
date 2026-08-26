"""Tests for MCP auto-instrumentation.

All tests use mocked MCP SDK objects -- the real ``mcp`` package is
**not** required.  We simulate the module structure that
:func:`instrument_mcp` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Generator
from contextlib import suppress
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

    def __init__(
        self,
        *,
        is_error: bool = False,
        text: str = "result",
        structured_content: dict[str, Any] | None = None,
    ) -> None:
        self.isError = is_error
        self.content = [{"type": "text", "text": text}]
        self.structuredContent = structured_content


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
    original_modules = {
        key: value
        for key, value in sys.modules.items()
        if key == "mcp" or key.startswith("mcp.")
    }
    for key in original_modules:
        sys.modules.pop(key, None)
    _install_fake_mcp()
    yield
    from dexcost.instruments.mcp import uninstrument_mcp

    uninstrument_mcp()
    for key in list(sys.modules):
        if key == "mcp" or key.startswith("mcp."):
            sys.modules.pop(key, None)
    sys.modules.update(original_modules)


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
            asyncio.run(session.call_tool("tavily_search", {"q": "test"}))

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
            asyncio.run(session.call_tool("brave_web_search", {"q": "hello"}))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        details = events[0].details
        assert details["mcp_tool"] == "brave_web_search"
        assert "mcp_server" in details
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], int)
        assert details["is_error"] is False

    def test_latency_is_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Latency is measured and recorded in details."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="latency_test") as task:
            asyncio.run(session.call_tool("some_tool"))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms >= 0

    def test_error_tool_call_tracked(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When call_tool raises, the error event is still recorded."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        async def _failing_call(self: Any, name: str, arguments: Any = None) -> Any:
            raise ConnectionError("MCP server unreachable")

        ClientSession.call_tool = _failing_call  # type: ignore[assignment]

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="error_test") as task, pytest.raises(ConnectionError):
            asyncio.run(session.call_tool("broken_tool"))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["is_error"] is True
        assert events[0].service_name == "mcp:broken_tool"
        # A raised call carries the exception identity, canonicalised.
        assert events[0].details["error_type"] == "connectionerror"

    def test_mcp_result_error_flag(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When MCP result.isError is True, details.is_error reflects it."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        async def _error_result(self: Any, name: str, arguments: Any = None) -> Any:
            return _FakeCallToolResult(is_error=True)

        ClientSession.call_tool = _error_result  # type: ignore[assignment]

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="result_error_test") as task:
            asyncio.run(session.call_tool("failing_tool"))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["is_error"] is True
        assert events[0].details["error_type"] == "tool_error"

    def test_invocation_context_v3_usage_and_private_payload_contract(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from mcp.client.session import ClientSession

        from dexcost.capabilities import capability_context
        from dexcost.idempotency import idempotency_key
        from dexcost.instruments.mcp import instrument_mcp
        from dexcost.models.capability import CapabilityIdentity

        private_argument = "private-mcp-tool-argument"
        private_result = "private-mcp-tool-result"

        async def call_tool(self: Any, name: str, arguments: Any = None) -> Any:
            result = _FakeCallToolResult()
            result.content = [{"type": "text", "text": private_result}]
            return result

        ClientSession.call_tool = call_tool  # type: ignore[assignment]
        instrument_mcp(tracker)
        session = ClientSession()
        capability = CapabilityIdentity(
            name="research.lookup",
            kind="tool",
            source="project",
            source_id="research.lookup/v1",
            invocation="automatic",
        )

        with tracker.task(task_type="mcp_context") as task:
            with capability_context(capability), idempotency_key("private-mcp-idempotency"):
                operation = session.call_tool("safe_lookup", {"query": private_argument})
            asyncio.run(operation)

        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.provider == "mcp"
        assert event.details["attribution_component"] == "external"
        assert event.details["attribution_operation_name"] == "mcp.call_tool"
        assert event.details["attribution_operation_status"] == "succeeded"
        assert event.details["attribution_usage_lines"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert event.details["attribution_capability"] == capability.to_dict()
        assert len(event.details["_dexcost_idempotency_sha256"]) == 64
        persisted = json.dumps(event.to_dict())
        for secret in (private_argument, private_result, "private-mcp-idempotency"):
            assert secret not in persisted


# ---------------------------------------------------------------------------
# attribution-v3 identity: resource type "tool" + failed operations
# ---------------------------------------------------------------------------


async def _succeeding_call(self: Any, name: str, arguments: Any = None) -> Any:
    """A tool call that returns a normal, non-error result."""
    return _FakeCallToolResult()


def _call(tracker: CostTracker, tool_name: str, call_tool: Any = None) -> Any:
    """Run one instrumented MCP tool call and return the recorded event.

    Always installs an explicit ``call_tool`` and puts the class attribute
    back exactly as it was found, so these tests neither depend on nor add to
    the leakage between the older tests in this module.
    """
    from mcp.client.session import ClientSession

    from dexcost.instruments.mcp import instrument_mcp, uninstrument_mcp

    previous = ClientSession.call_tool
    ClientSession.call_tool = call_tool or _succeeding_call  # type: ignore[assignment]
    try:
        instrument_mcp(tracker)
        session = ClientSession()
        with tracker.task(task_type="v3_identity") as task, suppress(Exception):
            asyncio.run(session.call_tool(tool_name))
    finally:
        # uninstrument first: it restores whatever was installed when the
        # patch was applied, which would otherwise undo the line below.
        uninstrument_mcp()
        ClientSession.call_tool = previous  # type: ignore[assignment]

    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return events[0]


class TestMCPToolIdentityV3:
    """The tool is the resource; failures become failed operations."""

    def test_resource_is_the_tool(self, tracker: CostTracker) -> None:
        """A successful tool call converts to resource={'type':'tool','id':<tool>}."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        event = _call(tracker, "tavily_search")
        assert event.details["attribution_resource_type"] == "tool"
        assert event.details["attribution_resource_id"] == "tavily_search"

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["resource"] == {"type": "tool", "id": "tavily_search"}
        assert converted["capability"] == {
            "name": "tavily_search",
            "kind": "tool",
            "invocation": "explicit",
        }

    def test_provider_identity_is_unchanged(self, tracker: CostTracker) -> None:
        """provider name/service keep their existing mcp:<tool> derivation."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        converted = to_attribution_observation_v3(_call(tracker, "brave_web_search"))
        assert converted is not None
        assert converted["provider"]["name"] == "mcp"
        assert converted["provider"]["service"] == "brave_web_search"

    def test_successful_call_has_no_error(self, tracker: CostTracker) -> None:
        """A succeeded operation never carries operation.error."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        converted = to_attribution_observation_v3(_call(tracker, "tavily_search"))
        assert converted is not None
        assert converted["operation"]["status"] == "succeeded"
        assert "error" not in converted["operation"]

    def test_is_error_result_maps_to_failed_tool_error(self, tracker: CostTracker) -> None:
        """result.isError -> status 'failed' + operation.error.type 'tool_error'."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        async def _error_result(self: Any, name: str, arguments: Any = None) -> Any:
            return _FakeCallToolResult(is_error=True)

        converted = to_attribution_observation_v3(_call(tracker, "failing_tool", _error_result))
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"] == {"type": "tool_error"}
        assert converted["resource"] == {"type": "tool", "id": "failing_tool"}

    def test_snake_case_is_error_result_is_honoured(self, tracker: CostTracker) -> None:
        """A client that spells the flag ``is_error`` is handled too."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        class _SnakeCaseResult:
            is_error = True

        async def _error_result(self: Any, name: str, arguments: Any = None) -> Any:
            return _SnakeCaseResult()

        converted = to_attribution_observation_v3(_call(tracker, "snake_tool", _error_result))
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"] == {"type": "tool_error"}

    def test_raised_call_maps_to_failed_with_exception_identity(
        self, tracker: CostTracker
    ) -> None:
        """A raising tool call becomes a failed operation naming the exception."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        async def _failing_call(self: Any, name: str, arguments: Any = None) -> Any:
            raise ConnectionError("MCP server unreachable")

        converted = to_attribution_observation_v3(_call(tracker, "broken_tool", _failing_call))
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"] == {"type": "connectionerror"}


# ---------------------------------------------------------------------------
# Cost resolution tests
# ---------------------------------------------------------------------------


class TestCostResolution:
    """Verify three-tier cost resolution."""

    def test_rate_registry_mcp_prefix(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When a rate is registered for 'mcp:<tool>', that cost is used."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        tracker.register_rate("mcp:tavily_search", per="call", cost_usd="0.008")
        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="rate_test") as task:
            asyncio.run(session.call_tool("tavily_search", {"q": "test"}))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.008")
        assert events[0].cost_confidence == "computed"
        assert events[0].pricing_source == "rate_registry"

    def test_unpublished_legacy_alias_is_not_guessed(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """An ambiguous code-era alias cannot apply an unrelated service rate."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        # No signed catalog binds this tool to the service key.
        tracker.register_rate("brave_search", per="call", cost_usd="0.005")
        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="catalog_test") as task:
            asyncio.run(session.call_tool("brave_web_search", {"q": "test"}))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0")
        assert events[0].cost_confidence == "unknown"

    def test_signed_service_catalog_alias_precedes_legacy_code_map(
        self, tracker: CostTracker
    ) -> None:
        """A server-distributed MCP alias resolves without an SDK code update."""
        from dexcost.service_catalog import ServiceCatalog

        tracker._service_catalog = ServiceCatalog(
            data={
                "example_api": {
                    "display_name": "Example API",
                    "domains": ["api.example.test"],
                    "mcp_tools": ["server_defined_tool"],
                    "category": "test",
                    "pricing_model": "per_request",
                    "cost_per_request_usd": "0.003",
                    "cost_extraction": {"type": "fixed"},
                    "source": "https://example.test/pricing",
                    "last_verified": "2026-08-24",
                }
            },
            catalog_version="signed-test",
        )
        tracker.register_rate("example_api", per="call", cost_usd="0.003")

        event = _call(tracker, "server_defined_tool")
        assert event.cost_usd == Decimal("0.003")
        assert event.cost_confidence == "computed"

    def test_credit_rate_uses_provider_reported_credit_quantity(
        self, tracker: CostTracker
    ) -> None:
        """A per-credit rate multiplies explicit provider result evidence."""
        from dexcost.attribution.v3_convert import to_attribution_observation_v3

        async def credit_result(
            self: Any, name: str, arguments: Any = None
        ) -> _FakeCallToolResult:
            return _FakeCallToolResult(
                text=json.dumps(
                    {
                        "creditsUsed": 7,
                        "private": "private-firecrawl-result",
                    }
                )
            )

        tracker.register_rate("mcp:firecrawl_crawl", per="credit", cost_usd="0.002")
        event = _call(tracker, "firecrawl_crawl", credit_result)
        assert event.cost_usd == Decimal("0.014")
        assert event.cost_confidence == "computed"
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert {
            (line["metric"], line["quantity"], line["unit"])
            for line in converted["usage"]
        } == {
            ("request_count", "1", "Requests"),
            ("credit_count", "7", "Credits"),
        }
        assert "private-firecrawl-result" not in json.dumps(event.to_dict())

    def test_non_call_rate_without_quantity_is_not_mispriced(
        self, tracker: CostTracker
    ) -> None:
        """A per-page rate is never silently treated as one page per call."""
        tracker.register_rate("mcp:firecrawl_crawl", per="page", cost_usd="0.002")
        event = _call(tracker, "firecrawl_crawl")
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.pricing_source == "unknown"

    def test_conflicting_credit_fields_remain_unpriced(
        self, tracker: CostTracker
    ) -> None:
        """Conflicting MCP result quantities are not resolved heuristically."""
        async def conflicting_result(
            self: Any, name: str, arguments: Any = None
        ) -> _FakeCallToolResult:
            return _FakeCallToolResult(
                structured_content={"creditsUsed": 2, "usage": {"credits": 3}}
            )

        tracker.register_rate("mcp:firecrawl_crawl", per="credit", cost_usd="0.002")
        event = _call(tracker, "firecrawl_crawl", conflicting_result)
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert [
            line for line in event.details["attribution_usage_lines"]
            if line["metric"] == "credit_count"
        ] == []

    def test_unknown_tool_zero_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Unknown tools get cost=0, confidence='unknown'."""
        from mcp.client.session import ClientSession

        from dexcost.instruments.mcp import instrument_mcp

        instrument_mcp(tracker)
        session = ClientSession()

        with tracker.task(task_type="unknown_test") as task:
            asyncio.run(session.call_tool("my_custom_tool"))

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
