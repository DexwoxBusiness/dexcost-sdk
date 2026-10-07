"""Bounded native Converse meters. Financial rules remain server-owned JSON."""

from __future__ import annotations

from typing import Any

from dexcost.instruments._provider_metering import (
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)

NOVA_MODELS = {"amazon.nova-micro-v1:0", "amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"}
# Source region is us-east-1; these exact profiles route globally, not in-region.
CLAUDE_GLOBAL_MODELS = {
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}
CONVERSE_MODELS = NOVA_MODELS | CLAUDE_GLOBAL_MODELS


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991
    )


def _text_blocks(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(block, dict) and set(block) == {"text"} and isinstance(block["text"], str)
        for block in value
    )


def request_eligible(client: Any, body: dict[str, Any]) -> bool:
    return (
        getattr(getattr(client, "meta", None), "region_name", None) == "us-east-1"
        and getattr(getattr(client, "_endpoint", None), "host", None)
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
        and body.get("modelId") in CONVERSE_MODELS
        and (body.get("serviceTier") is None or body["serviceTier"].get("type") == "default")
        and (
            body.get("performanceConfig") is None
            or body["performanceConfig"].get("latency") == "standard"
        )
        and all(
            body.get(key) is None
            for key in (
                "guardrailConfig",
                "additionalModelRequestFields",
                "promptVariables",
                "toolConfig",
            )
        )
        and isinstance(body.get("messages"), list)
        and bool(body["messages"])
        and all(
            isinstance(item, dict) and _text_blocks(item.get("content"))
            for item in body["messages"]
        )
        and (body.get("system") is None or _text_blocks(body["system"]))
    )


def measurement(body: dict[str, Any], response: Any, eligible: bool) -> OperationMeasurement:
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    valid = all(_count(usage.get(key)) for key in ("inputTokens", "outputTokens", "totalTokens"))
    valid = valid and usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"]
    no_cache = all(
        usage.get(key) is None or (type(usage[key]) is int and usage[key] == 0)
        for key in ("cacheReadInputTokens", "cacheWriteInputTokens")
    )
    no_cache = no_cache and (usage.get("cacheDetails") is None or usage["cacheDetails"] == [])
    record_id = _mapping(response.get("ResponseMetadata")).get("RequestId")
    record_id = record_id if isinstance(record_id, str) and 0 < len(record_id) <= 256 else None
    priced = (
        eligible
        and valid
        and no_cache
        and record_id is not None
        and _mapping(response.get("serviceTier")).get("type") == "default"
        and _mapping(response.get("performanceConfig")).get("latency") == "standard"
        and response.get("trace") is None
        and response.get("stopReason") in ("end_turn", "max_tokens", "stop_sequence")
    )
    return OperationMeasurement(
        pricing_usage={},
        usage_lines=tuple(
            ProviderUsageLine(metric, usage[key], "Tokens")
            for key, metric in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
            )
        )
        if valid
        else (),
        response_model=body.get("modelId"),
        provider_record_id=record_id,
        provider_region="us-east-1" if eligible else None,
        task_input_tokens=usage.get("inputTokens") if valid else None,
        task_output_tokens=usage.get("outputTokens") if valid else None,
        billing_dimensions=(
            (
                "bedrock_pricing_lane",
                "us_east_1_claude_global_standard_no_cache"
                if body.get("modelId") in CLAUDE_GLOBAL_MODELS
                else "us_east_1_nova_standard_no_cache",
            ),
        )
        if priced
        else (),
    )


class StreamMeter:
    def __init__(self, body: dict[str, Any], eligible: bool, request_id: Any) -> None:
        self.body, self.eligible, self.request_id = body, eligible, request_id
        self.metadata: Any = None
        self.stop_reason: Any = None
        self.invalid = False

    def observe(self, value: Any) -> None:
        if not isinstance(value, dict) or any(key.lower().endswith("exception") for key in value):
            self.invalid = True
            return
        if "messageStop" in value:
            if self.stop_reason is not None:
                self.invalid = True
            self.stop_reason = value["messageStop"].get("stopReason")
        if "metadata" in value:
            if self.metadata is not None or self.stop_reason is None:
                self.invalid = True
            self.metadata = value["metadata"]

    def measurement(self) -> OperationMeasurement:
        return measurement(
            self.body,
            {
                **(self.metadata or {}),
                "stopReason": self.stop_reason,
                "ResponseMetadata": {"RequestId": self.request_id},
            },
            self.eligible and not self.invalid,
        )

    def status(self) -> Any:
        return (
            "succeeded"
            if self.metadata is not None and self.stop_reason and not self.invalid
            else "unknown"
        )


class _EventIterator:
    """Keep EventStream.close available while adapting its iterable contract."""

    def __init__(self, stream: Any) -> None:
        self._stream, self._iterator = stream, iter(stream)

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        return next(self._iterator)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def converse_call(
    wrapped: Any,
    client: Any,
    args: Any,
    kwargs: Any,
    body: dict[str, Any],
    tracker: Any,
    streaming: bool,
) -> Any:
    try:
        eligible = request_eligible(client, body)
    except (AttributeError, TypeError, ValueError):
        eligible = False
    operation = "bedrock.converse_stream" if streaming else "bedrock.converse"
    session = ProviderOperationSession(
        tracker=tracker,
        task_type=operation,
        provider="aws_bedrock",
        service="bedrock_runtime",
        operation=operation,
        component="llm",
        model=body.get("modelId", "unknown"),
        event_type="llm_call",
    )
    try:
        result = wrapped(*args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if streaming and isinstance(result, dict) and result.get("stream") is not None:
        meter = StreamMeter(
            body, eligible, _mapping(result.get("ResponseMetadata")).get("RequestId")
        )
        session.release_context()
        return {
            **result,
            "stream": SyncProviderStream(
                _EventIterator(result["stream"]),
                session,
                observe=meter.observe,
                measurement=meter.measurement,
                completion_status=meter.status,
            ),
        }
    session.finish(measurement(body, result, eligible), "unknown" if streaming else "succeeded")
    return result
