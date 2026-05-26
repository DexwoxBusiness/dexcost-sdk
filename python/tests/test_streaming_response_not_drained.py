"""B11 regression — Sprint 2 Theme C / plan §3.1.2.

The HTTP adapter unconditionally called `response.json()` from the
catalog-matching path, draining streaming response bodies. Customer
code that iterates chunks (LLM streaming completions, SSE event
feeds, large downloads) silently received an empty iterator because
the SDK had already pulled the full body.

The fix: `_get_response_body` skips body extraction when the response
is streaming (Transfer-Encoding: chunked or Content-Type:
text/event-stream) and treats a missing Content-Length as "too large
to extract" rather than reading the full body. Catalog matchers fall
back to `fallback_credits` when the body is unavailable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from dexcost.adapters.http import _get_response_body


def _make_response(headers: dict[str, str], json_value=None) -> MagicMock:
    """Build a response mock that tracks whether .json() / .content was read."""
    resp = MagicMock()
    resp.headers = headers
    resp.json = MagicMock(return_value=json_value or {"usage": 1})
    return resp


def test_chunked_transfer_encoding_response_body_not_read() -> None:
    """Chunked responses (no Content-Length) must not be drained."""
    resp = _make_response({
        "Content-Type": "application/json",
        "Transfer-Encoding": "chunked",
    })
    result = _get_response_body(resp)
    assert result is None, (
        f"expected None for chunked response, got {result!r} "
        f"(body was drained — breaks streaming customers)"
    )
    assert not resp.json.called, "response.json() was called on a chunked response"


def test_sse_text_event_stream_response_body_not_read() -> None:
    """SSE (text/event-stream) responses must not be drained."""
    resp = _make_response({
        "Content-Type": "text/event-stream",
    })
    result = _get_response_body(resp)
    # Already returns None by Content-Type check, but assert .json() wasn't called either.
    assert result is None
    assert not resp.json.called


def test_missing_content_length_with_json_type_treated_as_too_large() -> None:
    """A JSON response with NO Content-Length header could be arbitrarily
    large — pre-fix the adapter fell through and read the whole body.
    Post-fix: treat unknown size as oversize and skip extraction."""
    resp = _make_response({
        "Content-Type": "application/json",
        # No Content-Length, no Transfer-Encoding — could be anything.
    })
    result = _get_response_body(resp)
    assert result is None
    assert not resp.json.called, (
        "response.json() called on a response with no Content-Length — "
        "may drain an arbitrarily large body"
    )


def test_small_json_response_with_content_length_still_extracted() -> None:
    """Regression guard: the non-streaming path still works."""
    resp = _make_response({
        "Content-Type": "application/json",
        "Content-Length": "42",
    }, json_value={"usage": {"tokens": 100}})
    result = _get_response_body(resp)
    assert result == {"usage": {"tokens": 100}}
    assert resp.json.called


def test_chunked_lowercase_header_name_still_caught() -> None:
    """HTTP headers are case-insensitive; chunked must be detected
    regardless of header-name case."""
    resp = _make_response({
        "content-type": "application/json",
        "transfer-encoding": "chunked",
    })
    result = _get_response_body(resp)
    assert result is None
    assert not resp.json.called
