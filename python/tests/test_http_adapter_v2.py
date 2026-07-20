"""Tests for the HTTP cost adapter v2 with service catalog integration.

Tests service catalog cost extraction, session auto-grouping, and
the rewritten HTTP adapter behaviour.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.adapters.http import (
    _handle_http_call,
    clear_domain_rates,
    clear_recorded_events,
    get_recorded_events,
    register_domain_rate,
    set_catalog,
    untrack_http,
)
from dexcost.attribution.convert import to_attribution_event_v2
from dexcost.context import clear_context, set_current_task, task_context
from dexcost.models.task import Task
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
            "name": "openai", "service": "embeddings", "record_id": "req-17"
        }
        assert wire["usage"] == [
            {"metric": "input_tokens", "quantity": "17", "unit": "Tokens"}
        ]
        assert "cost_evidence" not in wire

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
            _handle_http_call(url, response=_make_response(body=body))
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
        assert wire["usage"] == [
            {"metric": "characters", "quantity": "4", "unit": "Characters"}
        ]
        assert "cost_evidence" not in wire

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

    def test_deepgram_addons_are_separate_channel_second_lines(self) -> None:
        task = _make_task("transcription")
        response = _make_response(
            body={"metadata": {"request_id": "dg-addon", "duration": 10, "channels": 2}}
        )
        url = (
            "https://api.deepgram.com/v1/listen?model=nova-3&language=multi"
            "&multichannel=true&diarize_model=v2&redact=pci&keyterm=Acme"
        )
        with task_context(task):
            _handle_http_call(url, method="POST", response=response)
        wires = [to_attribution_event_v2(event) for event in get_recorded_events()]
        assert len(wires) == 4
        assert [wire["resource"]["id"] for wire in wires if wire is not None] == [
            "nova-3:multilingual",
            "speaker_diarization",
            "redaction",
            "keyterm_prompting",
        ]
        assert all(
            wire is not None and wire["usage"][0]["quantity"] == "20"
            and "cost_evidence" not in wire
            for wire in wires
        )

    def test_tavily_cost_from_response_body(self) -> None:
        """Tavily: cost extracted from response_body.usage.credits."""
        task = _make_task()
        response = _make_response(body={"usage": {"credits": 2}, "results": []})

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "external_cost"
        # 2 credits * $0.008 = $0.016
        assert event.cost_usd == Decimal("2") * Decimal("0.008")
        assert event.cost_confidence == "computed"
        assert event.pricing_source == "service_catalog"
        assert event.service_name == "Tavily Search"

    def test_pinecone_cost_from_response_body(self) -> None:
        """Pinecone: cost extracted from response_body.usage.readUnits."""
        task = _make_task()
        response = _make_response(
            body={"usage": {"readUnits": 10}, "matches": []},
        )

        with task_context(task):
            _handle_http_call(
                "https://my-index.svc.us-east1-gcp.pinecone.io/query",
                response=response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        # 10 * $0.000016 = $0.000160
        assert event.cost_usd == Decimal("10") * Decimal("0.000016")
        assert event.cost_confidence == "computed"
        assert event.service_name == "Pinecone"

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
            _handle_http_call("https://unknown-api.example.com/v1/data",
                              method="GET", request_headers={}, request_body_len=0,
                              response=response, latency_ms=5)

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
            _handle_http_call("https://unknown-api.example.com/v1/bulk",
                              method="GET", request_headers={}, request_body_len=0,
                              response=response, latency_ms=50)

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
        response = _make_response(body={"results": []})

        # No task context active
        _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        # Event should have a task_id (from the auto-created session)
        assert events[0].task_id is not None

    def test_session_groups_multiple_calls(self) -> None:
        """Multiple calls without explicit task share the same session task."""
        response1 = _make_response(body={"api_credits_used": 1})
        response2 = _make_response(body={"api_credits_used": 2})

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
        # Cost should use fallback (body wasn't parsed due to size)
        # Tavily has fallback_credits=1, so: 1 * $0.008 = $0.008
        assert events[0].cost_usd == Decimal("1") * Decimal("0.008")
        assert events[0].cost_confidence == "estimated"

    def test_non_json_response_body_skipped(self) -> None:
        """Non-JSON responses don't attempt body parsing."""
        task = _make_task()
        response = _make_response(
            content_type="text/html",
        )

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        # Body not parsed -> fallback credits used
        assert events[0].cost_usd == Decimal("1") * Decimal("0.008")
        assert events[0].cost_confidence == "estimated"

    def test_json_parse_failure_graceful(self) -> None:
        """If response.json() raises, extraction falls back gracefully."""
        task = _make_task()
        response = _make_response(content_type="application/json")
        response.json.side_effect = ValueError("Broken JSON")

        with task_context(task):
            _handle_http_call("https://api.tavily.com/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        # Falls back to fallback_credits
        assert events[0].cost_confidence == "estimated"


# ---------------------------------------------------------------------------
# Event field correctness
# ---------------------------------------------------------------------------


class TestEventFields:
    """Recorded events have correct fields."""

    def test_event_has_url_in_details(self) -> None:
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _handle_http_call("https://api.exa.ai/search?query=test", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].details["url"] == "https://api.exa.ai/search?query=test"

    def test_event_has_task_id_from_context(self) -> None:
        task = _make_task()
        response = _make_response(body={})

        with task_context(task):
            _handle_http_call("https://api.exa.ai/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].task_id == task.task_id

    def test_catalog_version_in_pricing_version(self) -> None:
        """Events from catalog matches include pricing_version."""
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _handle_http_call("https://api.exa.ai/search", response=response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].pricing_version is not None
        assert len(events[0].pricing_version) == 16
