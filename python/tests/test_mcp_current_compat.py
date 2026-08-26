"""Compatibility gates against the installed current MCP Python SDK."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.types import CallToolResult, TextContent

from dexcost.instruments.mcp import instrument_mcp, uninstrument_mcp
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_mcp() -> Generator[None, None, None]:
    uninstrument_mcp()
    yield
    uninstrument_mcp()


@pytest.fixture
def tracker(tmp_path: Any) -> Generator[CostTracker, None, None]:
    storage = SQLiteStorage(db_path=tmp_path / "mcp-current.db")
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    storage.close()


def _current_session(result: CallToolResult) -> tuple[ClientSession, list[str]]:
    session = object.__new__(ClientSession)
    requests: list[str] = []

    async def send_request(request: Any, result_type: Any, **kwargs: Any) -> Any:
        requests.append(request.model_dump_json(by_alias=True))
        assert result_type is CallToolResult
        return result

    async def validate_tool_result(name: str, response: Any) -> None:
        assert name == "tavily_search"
        assert response is result

    session.send_request = send_request  # type: ignore[assignment]
    session._validate_tool_result = validate_tool_result  # type: ignore[assignment]
    return session, requests


def test_current_mcp_128_call_tool_preserves_protocol_and_privacy(
    tracker: CostTracker,
) -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="private-current-mcp-result")],
        structuredContent={
            "private": "current-mcp-structured-result",
            "usage": {"credits": 2},
        },
    )
    session, requests = _current_session(result)
    tracker.register_rate("mcp:tavily_search", per="credit", cost_usd="0.008")
    instrument_mcp(tracker)

    with tracker.task(task_type="mcp.current.call_tool") as task:
        returned = asyncio.run(
            session.call_tool(
                "tavily_search",
                {"query": "private-current-mcp-argument"},
                meta={"private": "current-mcp-meta"},
            )
        )

    assert returned is result
    assert len(requests) == 1
    assert "private-current-mcp-argument" in requests[0]
    assert "current-mcp-meta" in requests[0]
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.provider == "mcp"
    assert event.service_name == "mcp:tavily_search"
    assert event.details["attribution_operation_status"] == "succeeded"
    assert event.details["attribution_resource_type"] == "tool"
    assert event.details["attribution_resource_id"] == "tavily_search"
    assert event.details["provider_usage_privacy"] == "quantities_only"
    assert event.cost_usd == Decimal("0.016")
    assert event.details["attribution_usage_lines"] == [
        {"metric": "request_count", "quantity": "1", "unit": "Requests"},
        {"metric": "credit_count", "quantity": "2", "unit": "Credits"},
    ]
    encoded = json.dumps(event.to_dict(), sort_keys=True)
    for secret in (
        "private-current-mcp-argument",
        "current-mcp-meta",
        "private-current-mcp-result",
        "current-mcp-structured-result",
    ):
        assert secret not in encoded


def test_current_mcp_128_protocol_error_is_failed_without_content(
    tracker: CostTracker,
) -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="private-current-mcp-error")],
        isError=True,
    )
    session, _ = _current_session(result)
    instrument_mcp(tracker)

    with tracker.task(task_type="mcp.current.error") as task:
        returned = asyncio.run(session.call_tool("tavily_search"))

    assert returned.isError is True
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.details["attribution_operation_status"] == "failed"
    assert event.details["error_type"] == "tool_error"
    assert "private-current-mcp-error" not in json.dumps(event.to_dict())
