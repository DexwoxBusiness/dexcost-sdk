"""Tests for the HTTP cost adapter v2 with service catalog integration.

Tests service catalog cost extraction, session auto-grouping, and
the rewritten HTTP adapter behaviour.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.adapters.http import (
    _aiohttp_wrapper,
    _botocore_wrapper,
    _handle_http_call,
    clear_domain_rates,
    clear_recorded_events,
    get_recorded_events,
    register_domain_rate,
    set_catalog,
    untrack_http,
)
from dexcost.attribution.convert import to_attribution_event_v2
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.context import (
    clear_context,
    set_current_task,
    suppress_network_event,
    task_context,
)
from dexcost.instruments._capture import provider_capture_scope
from dexcost.models.task import Task
from dexcost.service_catalog import ServiceCatalog
from dexcost.session import reset_session_manager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset adapter state before and after each test."""
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)  # Reset to force fresh catalog load
    yield
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)


def _make_task(task_type: str = "web_query") -> Task:
    return Task(task_type=task_type, customer_id="cust-1")


def _make_response(
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    content_type: str = "application/json",
    content_length: int | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Create a mock HTTP response."""
    response = MagicMock()
    response.status_code = status_code
    response.status = status_code

    # Build headers dict
    h: dict[str, str] = {}
    if content_type:
        h["content-type"] = content_type
    # Default Content-Length when a body is supplied — real HTTP servers
    # always set it for non-streaming responses, and B11 (Sprint 2 Theme
    # C / §3.1.2) treats missing Content-Length as "too large to read"
    # so tests that want JSON extraction need a value here.
    if content_length is not None:
        h["content-length"] = str(content_length)
    elif body is not None:
        h["content-length"] = "256"  # arbitrary small value below 1 MB
    if headers:
        h.update(headers)
    response.headers = h

    # Set up json() method
    if body is not None:
        response.json.return_value = body
    else:
        response.json.side_effect = ValueError("No JSON")

    return response


# ---------------------------------------------------------------------------
# Service catalog extraction tests
# ---------------------------------------------------------------------------


class TestKnownServiceExtraction:
    @pytest.mark.asyncio
    async def test_aiohttp_json_body_is_observed_when_caller_materialises_it(self) -> None:
        task = _make_task("embedding")

        class FakeAiohttpResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {
                    "content-type": "application/json",
                    "content-length": "128",
                    "x-request-id": "req-aiohttp-23",
                }

            async def json(self) -> dict[str, Any]:
                return {
                    "model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": 23, "total_tokens": 23},
                }

        response = FakeAiohttpResponse()

        async def wrapped(*args: Any, **kwargs: Any) -> FakeAiohttpResponse:
            return response

        with task_context(task):
            returned = await _aiohttp_wrapper(
                wrapped,
                None,
                ("POST", "https://api.openai.com/v1/embeddings"),
                {"json": {"model": "text-embedding-3-small", "input": "hello"}},
            )

        assert returned is response
        assert get_recorded_events() == []
        later_task = _make_task("unrelated-later-task")
        with task_context(later_task):
            assert (await response.json())["usage"]["total_tokens"] == 23
        event = get_recorded_events()[0]
        assert event.task_id == task.task_id
        assert task._network.finalize()["call_count"] == 1
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "openai",
            "service": "embeddings",
            "record_id": "req-aiohttp-23",
        }
        assert wire["usage"] == [
            {"metric": "input_tokens", "quantity": "23", "unit": "Tokens"}
        ]
        assert (await response.json())["usage"]["total_tokens"] == 23

    @pytest.mark.asyncio
    async def test_aiohttp_uses_effective_headers_for_razorpay_auth(self) -> None:
        task = _make_task("razorpay-aiohttp-capture")
        authorization = "Basic cnpwX2xpdmVfZml4dHVyZTpmaXh0dXJlX3NlY3JldA=="

        class RequestInfo:
            def __init__(self) -> None:
                self.headers = {"Authorization": authorization}

        class FakeAiohttpResponse:
            status = 200

            def __init__(self) -> None:
                self.request_info = RequestInfo()
                self.headers = {
                    "content-type": "application/json",
                    "content-length": "256",
                }

            async def json(self) -> dict[str, Any]:
                return {
                    "id": "pay_aiohttp_live_1",
                    "entity": "payment",
                    "currency": "INR",
                    "status": "captured",
                    "captured": True,
                    "fee": 236,
                }

        response = FakeAiohttpResponse()

        async def wrapped(*args: Any, **kwargs: Any) -> FakeAiohttpResponse:
            return response

        with task_context(task):
            await _aiohttp_wrapper(
                wrapped,
                None,
                ("GET", "https://api.razorpay.com/v1/payments/pay_aiohttp_live_1"),
                {},
            )
            assert get_recorded_events() == []
            await response.json()

        event = get_recorded_events()[0]
        assert event.details["attribution_observer_service"] == (
            "razorpay_captured_payment_fee"
        )
        assert event.details["provider_reported_cost_amount"] == "2.36"
        retained = json.dumps(event.details)
        assert "rzp_live_" not in retained
        assert "fixture_secret" not in retained

    def test_translation_query_is_metered_then_redacted_before_storage(self) -> None:
        task = _make_task("google-translate")
        private_text = "private customer text"
        url = (
            "https://translation.googleapis.com/language/translate/v2"
            f"?q={private_text.replace(' ', '%20')}&q=two&model=nmt"
        )

        with task_context(task):
            _handle_http_call(url, method="GET", response=_make_response(body={}))

        event = get_recorded_events()[0]
        assert event.details["attribution_usage_quantity"] == str(
            len(private_text) + len("two")
        )
        assert event.details["url"].count("q=REDACTED") == 2
        assert "model=nmt" in event.details["url"]
        assert private_text not in json.dumps(event.details)
        assert "private%20customer%20text" not in json.dumps(event.details)

    @pytest.mark.asyncio
    async def test_aiohttp_json_stream_is_not_drained_before_return(self) -> None:
        task = _make_task("embedding-stream")

        class FakeContent:
            def __init__(self) -> None:
                self._chunks = [b'{"usage":', b'{"prompt_tokens":23}}']

            async def iter_chunked(self, size: int) -> Any:
                del size
                for chunk in self._chunks:
                    yield chunk

        class FakeAiohttpResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {
                    "content-type": "application/json",
                    "content-length": "30",
                }
                self.content = FakeContent()
                self.json_calls = 0

            async def json(self) -> dict[str, Any]:
                self.json_calls += 1
                self.content._chunks.clear()
                return {"usage": {"prompt_tokens": 23}}

        response = FakeAiohttpResponse()

        async def wrapped(*args: Any, **kwargs: Any) -> FakeAiohttpResponse:
            return response

        with task_context(task):
            returned = await _aiohttp_wrapper(
                wrapped,
                None,
                ("POST", "https://api.openai.com/v1/embeddings"),
                {"json": {"model": "text-embedding-3-small", "input": "hello"}},
            )
            chunks = [chunk async for chunk in returned.content.iter_chunked(8)]

        assert response.json_calls == 0
        assert chunks == [b'{"usage":', b'{"prompt_tokens":23}}']
        assert get_recorded_events() == []

    @pytest.mark.asyncio
    async def test_aiohttp_deferred_body_still_accounts_network_immediately(self) -> None:
        task = _make_task("embedding-unread-body")
        response = _make_response(
            headers={"x-request-id": "req-aiohttp-unread"},
            body={"usage": {"prompt_tokens": 23}},
            content_length=128,
        )
        original_json = response.json

        async def wrapped(*args: Any, **kwargs: Any) -> MagicMock:
            return response

        with task_context(task):
            returned = await _aiohttp_wrapper(
                wrapped,
                None,
                ("POST", "https://api.openai.com/v1/embeddings"),
                {"json": {"model": "text-embedding-3-small", "input": "hello"}},
            )

        assert returned is response
        assert original_json.call_count == 0
        assert task._network.finalize()["call_count"] == 1
        assert get_recorded_events() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "content_length"),
        [(200, 200_000), (503, 128)],
        ids=["byte-threshold", "http-error"],
    )
    async def test_aiohttp_deferred_body_preserves_notable_network_event(
        self,
        status_code: int,
        content_length: int,
    ) -> None:
        task = _make_task("embedding-large-unread-body")
        response = _make_response(
            body={"usage": {"prompt_tokens": 23}},
            content_length=content_length,
            status_code=status_code,
        )
        original_json = response.json

        async def wrapped(*args: Any, **kwargs: Any) -> MagicMock:
            return response

        with task_context(task):
            await _aiohttp_wrapper(
                wrapped,
                None,
                ("POST", "https://api.openai.com/v1/embeddings"),
                {"json": {"model": "text-embedding-3-small", "input": "hello"}},
            )

        assert original_json.call_count == 0
        assert task._network.finalize()["call_count"] == 1
        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "network"
        assert events[0].task_id == task.task_id

    """HTTP calls to known services extract cost from response."""

    def test_openai_embedding_usage_has_no_synthetic_cost(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            headers={"x-request-id": "req-17"},
            body={
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 17, "total_tokens": 17},
            },
        )
        with task_context(task):
            _handle_http_call("https://api.openai.com/v1/embeddings", response=response)
        event = get_recorded_events()[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert wire is not None
        assert wire["provider"] == {
            "name": "openai",
            "service": "embeddings",
            "record_id": "req-17",
        }
        assert wire["usage"] == [{"metric": "input_tokens", "quantity": "17", "unit": "Tokens"}]
        assert "cost_evidence" not in wire

    def test_brave_observer_supersedes_legacy_domain_catalog(self) -> None:
        task = _make_task("search")
        with task_context(task):
            _handle_http_call(
                "https://api.search.brave.com/res/v1/web/search?q=dexcost",
                response=_make_response(body={"type": "search", "web": {"results": []}}),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.details["attribution_observer_service"] == "brave_search"
        assert wire is not None
        assert wire["provider"] == {"name": "brave", "service": "web_search"}
        assert wire["resource"] == {"type": "sku", "id": "search"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert "cost_evidence" not in wire

    def test_leonardo_provider_reported_dollar_charge_needs_no_catalog_math(
        self,
    ) -> None:
        task = _make_task("leonardo-generation")
        with task_context(task):
            _handle_http_call(
                "https://cloud.leonardo.ai/api/rest/v2/generationssync",
                method="POST",
                request_body={"model": "remove-bg", "parameters": {}},
                response=_make_response(
                    body={
                        "id": "leo-sync-http-1",
                        "blockedCount": 0,
                        "cost": {"amount": "0.1047", "unit": "DOLLARS"},
                        "results": [],
                    }
                ),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == Decimal("0.1047")
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "provider_response"
        assert (
            event.details["attribution_observer_service"]
            == "leonardo_generation_sync_cost"
        )
        assert "results" not in json.dumps(event.details)
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "leonardo_ai",
            "service": "production_api",
            "record_id": "leo-sync-http-1",
        }
        assert wire["resource"] == {"type": "model", "id": "remove-bg"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert wire["cost_evidence"] == {
            "amount": "0.1047",
            "currency": "USD",
            "source": "provider_reported",
            "confidence": "exact",
        }

    def test_mux_robots_records_terminal_provider_units_without_response_content(
        self,
    ) -> None:
        task = _make_task("mux-robots")
        with task_context(task):
            _handle_http_call(
                "https://api.mux.com/robots/v0/jobs/summarize/rjob-summary-1",
                method="GET",
                response=_make_response(
                    body={
                        "data": {
                            "id": "rjob-summary-1",
                            "workflow": "summarize",
                            "status": "completed",
                            "units_consumed": 650,
                            "outputs": {"title": "private output"},
                        }
                    }
                ),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        assert event.details["attribution_observer_service"] == "mux_robots_terminal_units"
        assert "private output" not in json.dumps(event.details)
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "mux",
            "service": "robots",
            "record_id": "rjob-summary-1",
        }
        assert wire["resource"] == {"type": "sku", "id": "summarize"}
        assert wire["usage"] == [
            {"metric": "credit_count", "quantity": "650", "unit": "Credits"}
        ]
        assert "cost_evidence" not in wire

    def test_paypal_order_capture_records_summed_fee_once_without_response_content(
        self,
    ) -> None:
        task = _make_task("paypal-capture")
        response_body = {
            "id": "PAYPAL-ORDER-HTTP-1",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "private_reference": "do-not-retain",
                    "payments": {
                        "captures": [
                            {
                                "id": "CAPTURE-HTTP-1",
                                "status": "COMPLETED",
                                "seller_receivable_breakdown": {
                                    "paypal_fee": {
                                        "currency_code": "USD",
                                        "value": "3.98",
                                    }
                                },
                            }
                        ]
                    },
                },
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "CAPTURE-HTTP-2",
                                "status": "COMPLETED",
                                "seller_receivable_breakdown": {
                                    "paypal_fee": {
                                        "currency_code": "USD",
                                        "value": "1.25",
                                    }
                                },
                            }
                        ]
                    }
                },
            ],
        }
        with task_context(task):
            for _ in range(2):
                _handle_http_call(
                    "https://api-m.paypal.com/v2/checkout/orders/PAYPAL-ORDER-HTTP-1/capture",
                    method="POST",
                    response=_make_response(body=response_body),
                )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == Decimal("5.23")
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "provider_response"
        assert event.details["attribution_observer_service"] == "paypal_order_capture_fee"
        assert "do-not-retain" not in json.dumps(event.details)
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "paypal",
            "service": "payment_processing",
            "record_id": "PAYPAL-ORDER-HTTP-1",
        }
        assert wire["resource"] == {"type": "sku", "id": "payment_capture"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert wire["cost_evidence"] == {
            "amount": "5.23",
            "currency": "USD",
            "source": "provider_reported",
            "confidence": "exact",
        }

    def test_razorpay_live_capture_records_native_fee_once_without_credentials(
        self,
    ) -> None:
        task = _make_task("razorpay-capture")
        authorization = (
            "Basic cnpwX2xpdmVfZml4dHVyZTpmaXh0dXJlX3NlY3JldA=="
        )
        response_body = {
            "id": "pay_http_live_1",
            "entity": "payment",
            "amount": 10000,
            "currency": "INR",
            "status": "captured",
            "captured": True,
            "fee": 236,
            "tax": 36,
            "notes": {"private": "do-not-retain"},
        }
        with task_context(task):
            for url, method in (
                (
                    "https://api.razorpay.com/v1/payments/pay_http_live_1/capture",
                    "POST",
                ),
                ("https://api.razorpay.com/v1/payments/pay_http_live_1", "GET"),
            ):
                _handle_http_call(
                    url,
                    method=method,
                    request_headers={"Authorization": authorization},
                    response=_make_response(body=response_body),
                )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        assert (
            event.details["attribution_observer_service"]
            == "razorpay_captured_payment_fee"
        )
        assert event.details["provider_reported_cost_amount"] == "2.36"
        assert event.details["provider_reported_cost_currency"] == "INR"
        retained = json.dumps(event.details)
        assert "do-not-retain" not in retained
        assert "fixture_secret" not in retained
        assert "rzp_live_" not in retained
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "razorpay",
            "service": "payment_processing",
            "record_id": "pay_http_live_1",
        }
        assert wire["resource"] == {"type": "sku", "id": "payment_capture"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert wire["cost_evidence"] == {
            "amount": "2.36",
            "currency": "INR",
            "source": "provider_reported",
            "confidence": "exact",
        }

    def test_runway_records_final_credits_once_without_response_content(self) -> None:
        task = _make_task("runway-generation")
        with task_context(task):
            for _ in range(2):
                _handle_http_call(
                    "https://api.dev.runwayml.com/v1/tasks/runway-task-1",
                    method="GET",
                    response=_make_response(
                        body={
                            "id": "runway-task-1",
                            "status": "SUCCEEDED",
                            "createdAt": "2026-09-03T01:15:00Z",
                            "cost": {"credits": 37.5},
                            "output": ["private-output-url"],
                        }
                    ),
                )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        assert (
            event.details["attribution_observer_service"]
            == "runway_terminal_task_credits"
        )
        assert "private-output-url" not in json.dumps(event.details)
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "runway",
            "service": "generation",
            "record_id": "runway-task-1",
        }
        assert wire["resource"] == {"type": "sku", "id": "generation"}
        assert wire["usage"] == [
            {"metric": "credit_count", "quantity": "37.5", "unit": "Credits"}
        ]
        assert "cost_evidence" not in wire

    def test_github_rest_api_is_usage_only_without_synthetic_money(self) -> None:
        task = _make_task("github-api")
        with task_context(task):
            _handle_http_call(
                "https://api.github.com/search/issues?q=repo:octocat/Hello-World",
                response=_make_response(body={"total_count": 1, "items": [{"id": 1}]}),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source != "service_catalog"
        assert event.details["attribution_observer_service"] == "github_api"
        assert wire is not None
        assert wire["provider"] == {"name": "github", "service": "rest_api"}
        assert wire["resource"] == {"type": "sku", "id": "rest_api_request"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert "cost_evidence" not in wire

    def test_failed_github_rest_api_request_does_not_restore_legacy_zero_price(
        self,
    ) -> None:
        task = _make_task("github-api")
        with task_context(task):
            _handle_http_call(
                "https://api.github.com/repos/octocat/private",
                response=_make_response(body={"message": "Forbidden"}, status_code=403),
            )

        events = get_recorded_events()
        assert all(
            event.details.get("attribution_observer_service") != "github_api"
            and event.pricing_source != "service_catalog"
            for event in events
        )

    def test_github_graphql_points_are_not_misclassified_as_rest_requests(
        self,
    ) -> None:
        task = _make_task("github-graphql")
        with task_context(task):
            _handle_http_call(
                "https://api.github.com/graphql",
                method="POST",
                request_body={"query": "query { viewer { login } }"},
                response=_make_response(
                    body={"data": {"viewer": {"login": "octocat"}}}
                ),
            )

        assert all(
            event.details.get("attribution_observer_service") != "github_api"
            and event.pricing_source != "service_catalog"
            for event in get_recorded_events()
        )

    def test_discord_and_gitlab_rest_usage_is_observed_without_synthetic_money(
        self,
    ) -> None:
        task = _make_task("developer-apis")
        with task_context(task):
            _handle_http_call(
                "https://discord.com/api/v10/channels/123/messages",
                response=_make_response(body={"ok": True}),
            )
            _handle_http_call(
                "https://gitlab.com/api/v4/projects?membership=true",
                response=_make_response(body=[{"id": 1, "name": "dexcost"}]),
            )

        events = get_recorded_events()
        assert len(events) == 2
        assert sorted(
            event.details["attribution_observer_service"] for event in events
        ) == ["discord_api", "gitlab_api"]
        for event in events:
            wire = to_attribution_event_v2(event)
            assert event.cost_usd == 0
            assert event.cost_confidence == "unknown"
            assert event.pricing_source != "service_catalog"
            assert wire is not None
            assert wire["resource"] == {"type": "sku", "id": "rest_api_request"}
            assert wire["usage"] == [
                {"metric": "request_count", "quantity": "1", "unit": "Requests"}
            ]
            assert "cost_evidence" not in wire

    def test_failed_discord_and_gitlab_requests_do_not_restore_legacy_zero_prices(
        self,
    ) -> None:
        task = _make_task("developer-apis")
        with task_context(task):
            _handle_http_call(
                "https://discord.com/api/v10/channels/123/messages",
                response=_make_response(body={"message": "Forbidden"}, status_code=403),
            )
            _handle_http_call(
                "https://gitlab.com/api/v4/projects/private",
                response=_make_response(body={"message": "Forbidden"}, status_code=403),
            )

        assert all(
            event.details.get("attribution_observer_service")
            not in {"discord_api", "gitlab_api"}
            and event.pricing_source != "service_catalog"
            for event in get_recorded_events()
        )

    def test_authenticated_jina_reader_usage_does_not_retain_credentials(self) -> None:
        task = _make_task("reader")
        with task_context(task):
            _handle_http_call(
                "https://r.jina.ai/https://example.com/article",
                request_headers={"Authorization": "Bearer must-not-be-recorded"},
                response=_make_response(headers={"x-usage-tokens": "257"}, body=None),
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].details["attribution_observer_service"] == "jina_reader"
        wire = to_attribution_event_v2(events[0])
        assert wire is not None
        assert wire["provider"] == {"name": "jina", "service": "reader"}
        assert wire["resource"] == {"type": "sku", "id": "standard_token"}
        assert wire["usage"] == [
            {"metric": "output_tokens", "quantity": "257", "unit": "Tokens"}
        ]
        assert "must-not-be-recorded" not in repr(events)

    def test_anonymous_jina_reader_usage_is_not_priced(self) -> None:
        task = _make_task("reader")
        with task_context(task):
            _handle_http_call(
                "https://r.jina.ai/http://example.com",
                response=_make_response(headers={"x-usage-tokens": "29"}, body=None),
            )

        assert not any(
            event.details.get("attribution_observer_service") == "jina_reader"
            or event.pricing_source == "service_catalog"
            for event in get_recorded_events()
        )

    def test_failed_brave_request_does_not_fall_back_to_legacy_price(self) -> None:
        task = _make_task("search")
        with task_context(task):
            _handle_http_call(
                "https://api.search.brave.com/res/v1/web/search?q=dexcost",
                response=_make_response(body={"error": "unavailable"}, status_code=503),
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "network"
        assert events[0].details.get("attribution_observer_service") is None
        assert events[0].pricing_source != "service_catalog"

    def test_exa_search_observer_emits_variant_without_sdk_money(self) -> None:
        task = _make_task("search")
        request_body = {"query": "DexCost", "type": "deep", "numResults": 10}
        with task_context(task):
            _handle_http_call(
                "https://api.exa.ai/search",
                method="POST",
                request_body=request_body,
                response=_make_response(
                    body={"requestId": "exa-deep-http-1", "results": []}
                ),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        assert wire is not None
        assert wire["provider"] == {
            "name": "exa",
            "service": "search_api",
            "record_id": "exa-deep-http-1",
        }
        assert wire["resource"] == {"type": "sku", "id": "deep"}
        assert wire["usage"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
        assert "cost_evidence" not in wire

    def test_exa_search_without_captured_request_body_fails_open(self) -> None:
        task = _make_task("search")
        with task_context(task):
            _handle_http_call(
                "https://api.exa.ai/search",
                method="POST",
                response=_make_response(
                    body={"requestId": "exa-unreadable-http", "results": []}
                ),
            )

        assert get_recorded_events() == []

    def test_botocore_transport_observes_amazon_translate_request_characters(self) -> None:
        task = _make_task("translation")
        request = MagicMock()
        request.url = "https://translate.us-east-1.amazonaws.com/"
        request.method = "POST"
        request.headers = {}
        request.body = json.dumps(
            {
                "Text": "Hello \ud83d\udc4b world",
                "SourceLanguageCode": "en",
                "TargetLanguageCode": "es",
            }
        ).encode()
        response = _make_response(
            body={
                "TranslatedText": "Hola mundo",
                "SourceLanguageCode": "en",
                "TargetLanguageCode": "es",
            },
            content_type="application/x-amz-json-1.1",
        )

        with task_context(task):
            returned = _botocore_wrapper(
                lambda *_args, **_kwargs: response,
                None,
                (request,),
                {},
            )

        assert returned is response
        observations = [
            event
            for event in get_recorded_events()
            if event.details.get("attribution_observer_service") == "aws_translate"
        ]
        assert len(observations) == 1
        wire = to_attribution_observation_v3(observations[0])
        assert wire is not None
        assert wire["provider"] == {"name": "aws", "service": "translate_text"}
        assert wire["resource"] == {"type": "sku", "id": "standard_text"}
        assert len(wire["usage"]) == 1
        assert {
            key: wire["usage"][0][key]
            for key in ("metric", "quantity", "unit")
        } == {"metric": "characters", "quantity": "13", "unit": "Characters"}

    def test_botocore_rekognition_observes_both_detect_labels_skus_and_region(
        self,
    ) -> None:
        task = _make_task("image-analysis")
        request = MagicMock()
        request.url = "https://rekognition.us-east-1.amazonaws.com/"
        request.method = "POST"
        request.headers = {
            "X-Amz-Target": "RekognitionService.DetectLabels",
            "Authorization": "AWS4-HMAC-SHA256 must-not-be-recorded",
        }
        request.body = json.dumps(
            {"Features": ["GENERAL_LABELS", "IMAGE_PROPERTIES"]}
        ).encode()
        response = _make_response(
            headers={"x-amzn-requestid": "rek-http-1"},
            body={"Labels": [], "ImageProperties": {}},
            content_type="application/x-amz-json-1.1",
        )

        with task_context(task):
            _botocore_wrapper(
                lambda *_args, **_kwargs: response,
                None,
                (request,),
                {},
            )

        observations = [
            event
            for event in get_recorded_events()
            if str(event.details.get("attribution_observer_service", "")).startswith(
                "aws_rekognition_"
            )
        ]
        assert len(observations) == 2
        assert {
            event.details["attribution_resource_id"] for event in observations
        } == {"group_2", "image_properties"}
        for event in observations:
            wire = to_attribution_observation_v3(event)
            assert wire is not None
            assert wire["provider"] == {
                "name": "aws",
                "service": "rekognition_image",
                "region": "us-east-1",
                "record_id": "rek-http-1",
            }
        assert "must-not-be-recorded" not in repr(observations)

    def test_http_transport_observes_azure_request_arrays_and_target_count(self) -> None:
        task = _make_task("translation")
        request_body = json.dumps([{"Text": "A\U0001f600"}, {"text": "é"}])

        with task_context(task):
            _handle_http_call(
                "https://api.cognitive.microsofttranslator.com/translate?"
                "api-version=3.0&to=es&to=ja",
                method="POST",
                request_body_len=len(request_body.encode()),
                request_body=request_body,
                response=_make_response(body={}),
            )

        observations = [
            event
            for event in get_recorded_events()
            if event.details.get("attribution_observer_service") == "azure_translator"
        ]
        assert len(observations) == 1
        wire = to_attribution_observation_v3(observations[0])
        assert wire is not None
        assert wire["provider"] == {"name": "azure", "service": "translate_text"}
        assert wire["resource"] == {"type": "sku", "id": "standard_text"}
        assert len(wire["usage"]) == 1
        assert {key: wire["usage"][0][key] for key in ("metric", "quantity", "unit")} == {
            "metric": "characters",
            "quantity": "8",
            "unit": "Characters",
        }

    def test_observer_endpoint_boundary_does_not_fall_back_to_legacy_price(self) -> None:
        task = _make_task("search")
        with task_context(task):
            _handle_http_call(
                "https://www.googleapis.com/customsearch/v1/siterestrict?q=dexcost",
                response=_make_response(body={"kind": "customsearch#search", "items": []}),
            )

        assert get_recorded_events() == []

    def test_user_override_remains_authoritative_on_observer_endpoint_boundary(
        self,
    ) -> None:
        catalog = ServiceCatalog()
        catalog.register_override("google_custom_search", Decimal("0.05"), per="request")
        set_catalog(catalog)
        task = _make_task("search")

        with task_context(task):
            _handle_http_call(
                "https://www.googleapis.com/customsearch/v1/siterestrict?q=dexcost",
                response=_make_response(body={"kind": "customsearch#search", "items": []}),
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.05")
        assert events[0].pricing_source == "user_override"
        assert events[0].pricing_version is None

    def test_exa_user_catalog_override_remains_authoritative(self) -> None:
        catalog = ServiceCatalog()
        catalog.register_override("exa_search", Decimal("0.05"), per="request")
        set_catalog(catalog)
        task = _make_task("search")

        with task_context(task):
            _handle_http_call(
                "https://api.exa.ai/search",
                method="POST",
                response=_make_response(body={"requestId": "exa-override", "results": []}),
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == Decimal("0.05")
        assert event.pricing_source == "user_override"
        assert event.pricing_version is None
        assert wire is not None
        assert wire["cost_evidence"] == {
            "amount": "0.05",
            "currency": "USD",
            "source": "manual",
            "confidence": "computed",
        }

    def test_provider_suppression_precedes_usage_observer(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            body={
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 17, "total_tokens": 17},
            },
        )

        with task_context(task), suppress_network_event():
            _handle_http_call(
                "https://api.openai.com/v1/embeddings",
                method="POST",
                request_body_len=31,
                response=response,
            )

        assert get_recorded_events() == []
        counters = task._network.finalize()
        assert counters["call_count"] == 1
        assert counters["bytes_in"] > 0
        assert counters["bytes_out"] >= 31

    def test_provider_capture_owner_suppresses_usage_observer(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            body={
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 17, "total_tokens": 17},
            },
        )

        with task_context(task), provider_capture_scope("openai") as claimed:
            assert claimed is True
            _handle_http_call(
                "https://api.openai.com/v1/embeddings",
                method="POST",
                request_body_len=31,
                response=response,
            )

        assert get_recorded_events() == []
        assert task._network.finalize()["call_count"] == 1

    def test_failed_provider_response_is_not_observed(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            body={"model": "text-embedding-3-small", "usage": {"total_tokens": 17}},
            status_code=500,
        )
        with task_context(task):
            _handle_http_call("https://api.openai.com/v1/embeddings", response=response)
        assert not any(
            event.details.get("attribution_observer_service") == "openai_embeddings"
            for event in get_recorded_events()
        )

    @pytest.mark.parametrize(
        ("url", "body", "observer_service", "provider"),
        [
            (
                "https://api.cohere.com/v2/embed",
                {"id": "cohere-1", "meta": {"billed_units": {"input_tokens": 29}}},
                "cohere_embed",
                "cohere",
            ),
            (
                "https://api.jina.ai/v1/embeddings",
                {"model": "jina-embeddings-v3", "usage": {"total_tokens": 53}},
                "jina_embeddings",
                "jina",
            ),
        ],
    )
    def test_observer_endpoint_is_not_claimed_by_rerank_catalog_fallback(
        self,
        url: str,
        body: dict[str, Any],
        observer_service: str,
        provider: str,
    ) -> None:
        task = _make_task("embedding")
        with task_context(task):
            _handle_http_call(
                url,
                request_body={"model": "embed-v4.0"} if provider == "cohere" else None,
                response=_make_response(body=body),
            )
        event = get_recorded_events()[0]
        wire = to_attribution_event_v2(event)
        assert event.cost_usd == 0
        assert event.details["attribution_observer_service"] == observer_service
        assert wire is not None
        assert wire["provider"]["name"] == provider
        assert "cost_evidence" not in wire

    def test_deepgram_duration_is_speech_to_text_seconds(self) -> None:
        task = _make_task("transcription")
        response = _make_response(
            body={"metadata": {"request_id": "dg-25", "duration": 25.933313}}
        )
        with task_context(task):
            _handle_http_call("https://api.deepgram.com/v1/listen", response=response)
        wire = to_attribution_event_v2(get_recorded_events()[0])
        assert wire is not None
        assert wire["component"] == "speech_to_text"
        assert wire["usage"] == [
            {"metric": "audio_seconds", "quantity": "25.933313", "unit": "Seconds"}
        ]
        assert wire["provider"]["record_id"] == "dg-25"
        assert wire["provider"]["service"] == "speech_to_text_pre_recorded"
        assert wire["resource"] == {"type": "sku", "id": "base-general:monolingual"}
        assert wire["usage_period"]["end_at"] is not None
        assert "cost_evidence" not in wire

    def test_openai_tts_request_characters_are_text_to_speech_usage(self) -> None:
        task = _make_task("speech")
        response = _make_response(
            headers={"x-request-id": "req-tts-4"},
            content_type="audio/mpeg",
            content_length=4,
        )
        with task_context(task):
            _handle_http_call(
                "https://api.openai.com/v1/audio/speech",
                method="POST",
                request_body={"model": "tts-1-hd", "input": "Hi 🌍"},
                response=response,
            )
        wire = to_attribution_event_v2(get_recorded_events()[0])
        assert wire is not None
        assert wire["component"] == "text_to_speech"
        assert wire["provider"] == {
            "name": "openai",
            "service": "text_to_speech",
            "record_id": "req-tts-4",
        }
        assert wire["resource"] == {"type": "model", "id": "tts-1-hd"}
        assert wire["usage"] == [{"metric": "characters", "quantity": "4", "unit": "Characters"}]
        assert "cost_evidence" not in wire

    def test_elevenlabs_tts_uses_provider_billed_character_header(self) -> None:
        task = _make_task("speech")
        response = _make_response(
            headers={"character-cost": "11", "x-trace-id": "el-trace-11"},
            content_type="audio/mpeg",
            content_length=4,
        )
        with task_context(task):
            _handle_http_call(
                "https://api.elevenlabs.io/v1/text-to-speech/voice_123/stream",
                method="POST",
                request_body={"model_id": "eleven_flash_v2_5", "text": "Not eleven chars"},
                response=response,
            )
        event = get_recorded_events()[0]
        assert "Not eleven chars" not in json.dumps(event.details)
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["component"] == "text_to_speech"
        assert wire["provider"] == {
            "name": "elevenlabs",
            "service": "text_to_speech",
            "record_id": "el-trace-11",
        }
        assert wire["resource"] == {"type": "model", "id": "eleven_flash_v2_5"}
        assert wire["usage"] == [{"metric": "characters", "quantity": "11", "unit": "Characters"}]
        assert "cost_evidence" not in wire
        v3 = to_attribution_observation_v3(event)
        assert v3 is not None
        assert v3["schema_version"] == "3"
        assert v3["component"] == "text_to_speech"
        assert v3["provider"] == {
            "name": "elevenlabs",
            "service": "text_to_speech",
            "record_id": "el-trace-11",
        }
        assert v3["resource"] == {"type": "model", "id": "eleven_flash_v2_5"}
        assert v3["usage"][0]["metric"] == "characters"
        assert v3["usage"][0]["quantity"] == "11"
        assert v3["usage"][0]["unit"] == "Characters"
        assert v3["usage"][0]["dimensions"] == [
            {"key": "voice_id", "value": {"type": "string", "value": "voice_123"}}
        ]

    def test_cohere_request_model_reaches_attribution_v2(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            body={"id": "cohere-29", "meta": {"billed_units": {"input_tokens": 29}}}
        )
        with task_context(task):
            _handle_http_call(
                "https://api.cohere.com/v2/embed",
                method="POST",
                request_body={"model": "embed-v4.0", "texts": ["hello"]},
                response=response,
            )
        wire = to_attribution_event_v2(get_recorded_events()[0])
        assert wire is not None
        assert wire["resource"] == {"type": "model", "id": "embed-v4.0"}

    def test_cohere_mixed_usage_emits_distinct_text_and_image_token_events(self) -> None:
        task = _make_task("embedding")
        response = _make_response(
            body={
                "id": "cohere-mixed-1",
                "meta": {
                    "billed_units": {
                        "input_tokens": 29,
                        "image_tokens": 4096,
                        "images": 1,
                    }
                },
            }
        )
        with task_context(task):
            _handle_http_call(
                "https://api.cohere.com/v2/embed",
                method="POST",
                request_body={"model": "embed-v4.0", "inputs": []},
                response=response,
            )

        events = sorted(
            get_recorded_events(),
            key=lambda event: str(event.details["attribution_observer_service"]),
        )
        assert len(events) == 2
        assert len({event.event_id for event in events}) == 2
        wires = [to_attribution_event_v2(event) for event in events]
        assert [wire["usage"][0] for wire in wires if wire is not None] == [
            {"metric": "input_tokens", "quantity": "29", "unit": "Tokens"},
            {"metric": "input_image_tokens", "quantity": "4096", "unit": "Tokens"},
        ]

    def test_deepgram_addons_are_separate_channel_second_lines(self) -> None:
        task = _make_task("transcription")
        response = _make_response(
            body={"metadata": {"request_id": "dg-addon", "duration": 10, "channels": 2}}
        )
        url = (
            "https://api.deepgram.com/v1/listen?model=nova-3&language=multi"
            "&multichannel=true&diarize_model=v2&redact=pci&keyterm=Acme"
            "&detect_entities=true"
        )
        with task_context(task):
            _handle_http_call(url, method="POST", response=response)
        wires = [to_attribution_event_v2(event) for event in get_recorded_events()]
        assert len(wires) == 5
        assert [wire["resource"]["id"] for wire in wires if wire is not None] == [
            "nova-3:multilingual",
            "speaker_diarization",
            "redaction",
            "keyterm_prompting",
            "entity_detection",
        ]
        assert all(
            wire is not None
            and wire["usage"][0]["quantity"] == "20"
            and "cost_evidence" not in wire
            for wire in wires
        )

    def test_tavily_usage_from_response_body(self) -> None:
        """Tavily: billed credits are observed without SDK-side money."""
        task = _make_task()
        response = _make_response(
            body={"request_id": "tavily-2", "usage": {"credits": 2}, "results": []}
        )

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "external_cost"
        assert event.cost_usd == 0
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        wire = to_attribution_event_v2(event)
        assert wire is not None
        assert wire["provider"] == {
            "name": "tavily",
            "service": "search_api",
            "record_id": "tavily-2",
        }
        assert wire["resource"] == {"type": "sku", "id": "api_credit"}
        assert wire["usage"] == [
            {"metric": "credit_count", "quantity": "2", "unit": "Credits"}
        ]
        assert "cost_evidence" not in wire

    def test_provider_suppression_precedes_service_catalog(self) -> None:
        task = _make_task()
        response = _make_response(body={"usage": {"credits": 2}, "results": []})

        with task_context(task), suppress_network_event():
            _handle_http_call(
                "https://api.tavily.com/search",
                method="POST",
                request_body_len=19,
                response=response,
            )

        assert get_recorded_events() == []
        counters = task._network.finalize()
        assert counters["call_count"] == 1
        assert counters["bytes_in"] > 0
        assert counters["bytes_out"] >= 19

    def test_pinecone_rounded_read_units_are_observed_without_sdk_money(self) -> None:
        task = _make_task()
        response = _make_response(
            body={"usage": {"readUnits": 10}, "matches": []},
        )

        with task_context(task):
            _handle_http_call(
                "https://my-index.svc.us-east1-gcp.pinecone.io/query",
                method="POST",
                response=response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.pricing_source is None
        assert event.provider == "pinecone"
        assert event.service_name == "vector_database"
        assert event.details["attribution_usage_quantity"] == "10"
        assert event.details["attribution_usage_metric"] == "pinecone.read_units_rounded"
        assert event.details["attribution_usage_unit"] == "ReadUnits"
        assert event.details["attribution_resource_id"] == (
            "my-index.svc.us-east1-gcp.pinecone.io"
        )

    def test_google_maps_endpoint_match(self) -> None:
        """Google Maps Geocoding: fixed cost via endpoint_match."""
        task = _make_task()
        response = _make_response(body={"results": [], "status": "OK"})

        with task_context(task):
            _handle_http_call(
                "https://maps.googleapis.com/maps/api/geocode/json?address=foo",
                response=response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.005")
        assert events[0].cost_confidence == "exact"


# ---------------------------------------------------------------------------
# Unknown domain tests
# ---------------------------------------------------------------------------


class TestUnknownDomain:
    """HTTP calls to unknown domains: noise-removal means no event for small calls."""

    def test_unknown_domain_small_response_emits_no_event(self) -> None:
        """Un-cataloged calls with a small body produce no event (noise removal).

        The old ``external_cost $0 / unknown`` event is replaced by nothing
        when the combined bytes are below the 100 KiB threshold and the
        response is successful.  Bytes are still recorded in task counters.
        """
        from dexcost.adapters.http import _handle_http_call

        task = _make_task()
        # response with no Content-Length → body_len=0 → well below threshold
        response = _make_response(body={"data": "hello"})

        with task_context(task):
            _handle_http_call(
                "https://unknown-api.example.com/v1/data",
                method="GET",
                request_headers={},
                request_body_len=0,
                response=response,
                latency_ms=5,
            )

        # No event — small successful call to un-cataloged domain.
        events = get_recorded_events()
        assert len(events) == 0

    def test_unknown_domain_large_response_emits_network_event(self) -> None:
        """Un-cataloged call above the byte threshold emits a ``network`` event."""
        from dexcost.adapters.http import _handle_http_call

        task = _make_task()
        # Simulate a response with Content-Length above the 100 KiB threshold.
        response = _make_response(body={"data": "x"}, content_length=200_000)

        with task_context(task):
            _handle_http_call(
                "https://unknown-api.example.com/v1/bulk",
                method="GET",
                request_headers={},
                request_body_len=0,
                response=response,
                latency_ms=50,
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "network"
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.service_name == "unknown-api.example.com"


# ---------------------------------------------------------------------------
# Auto-session tests
# ---------------------------------------------------------------------------


class TestAutoSession:
    """HTTP calls without explicit task create auto-sessions."""

    def test_creates_session_when_no_task(self) -> None:
        """Without an explicit task, a session task is auto-created."""
        response = _make_response(
            body={"request_id": "session-1", "usage": {"credits": 1}, "results": []}
        )

        # No task context active
        _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        # Event should have a task_id (from the auto-created session)
        assert events[0].task_id is not None

    def test_session_groups_multiple_calls(self) -> None:
        """Multiple calls without explicit task share the same session task."""
        response1 = _make_response(
            body={"request_id": "session-1", "usage": {"credits": 1}}
        )
        response2 = _make_response(
            body={"request_id": "session-2", "usage": {"credits": 2}}
        )

        _handle_http_call("https://api.tavily.com/search", response=response1)
        _handle_http_call("https://api.tavily.com/search", response=response2)

        events = get_recorded_events()
        assert len(events) == 2
        # Both events should have the same task_id
        assert events[0].task_id == events[1].task_id


# ---------------------------------------------------------------------------
# User override tests
# ---------------------------------------------------------------------------


class TestDomainRateOverride:
    """register_domain_rate overrides catalog rate."""

    def test_override_takes_precedence(self) -> None:
        register_domain_rate("api.tavily.com", cost_usd="0.50")

        task = _make_task()
        response = _make_response(body={"api_credits_used": 3})

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        # Should use the override rate, not the catalog
        assert events[0].cost_usd == Decimal("0.50")
        assert events[0].pricing_source == "manual"


# ---------------------------------------------------------------------------
# Response body edge cases
# ---------------------------------------------------------------------------


class TestResponseBodyEdgeCases:
    """Edge cases for response body parsing."""

    def test_large_response_body_not_parsed(self) -> None:
        """Responses > 1MB are not parsed for cost extraction."""
        task = _make_task()
        response = _make_response(
            body={"api_credits_used": 5},
            content_length=2_000_000,  # 2MB
        )

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "network"
        assert events[0].cost_usd == 0
        assert events[0].cost_confidence == "unknown"
        assert events[0].pricing_source != "service_catalog"

    def test_non_json_response_body_skipped(self) -> None:
        """Non-JSON responses don't attempt body parsing."""
        task = _make_task()
        response = _make_response(
            content_type="text/html",
        )

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert events == []

    def test_json_parse_failure_graceful(self) -> None:
        """If response.json() raises, extraction falls back gracefully."""
        task = _make_task()
        response = _make_response(content_type="application/json")
        response.json.side_effect = ValueError("Broken JSON")

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert events == []


# ---------------------------------------------------------------------------
# Event field correctness
# ---------------------------------------------------------------------------


class TestEventFields:
    """Recorded events have correct fields."""

    def test_event_has_url_in_details(self) -> None:
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _handle_http_call(
                "https://api.exa.ai/search?query=test",
                method="POST",
                request_body={"query": "test"},
                response=response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].details["url"] == "https://api.exa.ai/search?query=test"

    def test_event_has_task_id_from_context(self) -> None:
        task = _make_task()
        response = _make_response(body={})

        with task_context(task):
            _handle_http_call(
                "https://api.exa.ai/search",
                method="POST",
                request_body={"query": "test"},
                response=response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].task_id == task.task_id

    def test_catalog_version_in_pricing_version(self) -> None:
        """Events from catalog matches include pricing_version."""
        set_catalog(
            ServiceCatalog(
                data={
                    "_meta": {"version": "test"},
                    "fixed_test": {
                        "display_name": "Fixed Test API",
                        "domains": ["fixed.example.test"],
                        "category": "test",
                        "pricing_model": "per_request",
                        "cost_per_request_usd": "0.01",
                        "cost_extraction": {"type": "fixed"},
                        "source": "https://example.test/pricing",
                        "last_verified": "2026-09-01",
                    },
                }
            )
        )
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _handle_http_call("https://fixed.example.test/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].pricing_version is not None
        assert len(events[0].pricing_version) == 16
