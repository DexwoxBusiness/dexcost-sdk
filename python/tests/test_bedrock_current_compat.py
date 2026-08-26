"""Compatibility gates for current boto3 Bedrock Converse operations."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from collections.abc import Generator, Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _load_current_boto3() -> tuple[Any, type[Any]]:
    """Import the installed SDK after legacy fake-module tests have torn down."""
    if sys.modules.get("botocore") is None:
        for name in tuple(sys.modules):
            if name == "boto3" or name.startswith("boto3."):
                sys.modules.pop(name, None)
            if name == "botocore" or name.startswith("botocore."):
                sys.modules.pop(name, None)
    boto3 = pytest.importorskip("boto3")
    from botocore.client import BaseClient

    return boto3, BaseClient


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(db_path=tmp_path / "bedrock-current.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _reset_instrumentation() -> Iterator[None]:
    from dexcost.instruments.bedrock import uninstrument_bedrock

    uninstrument_bedrock()
    yield
    uninstrument_bedrock()


def _client(boto3: Any) -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_current_converse_records_disjoint_cache_tools_and_region_pricing(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)
    private_prompt = "private-current-bedrock-prompt"
    private_tool_name = "private_current_bedrock_tool"
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "private-tool-id",
                            "name": private_tool_name,
                            "input": {"query": private_prompt},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {
            "inputTokens": 10,
            "outputTokens": 3,
            "totalTokens": 19,
            "cacheReadInputTokens": 4,
            "cacheWriteInputTokens": 2,
        },
        "metrics": {"latencyMs": 1},
        "performanceConfig": {"latency": "standard"},
        "serviceTier": {"type": "default"},
        "ResponseMetadata": {"RequestId": "converse-current-1", "RetryAttempts": 2},
    }

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        assert operation_name == "Converse"
        return response

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    model = "us.anthropic.claude-opus-4-6-v1"
    with tracker.task(task_type="bedrock_current_converse") as task:
        actual = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": private_prompt}]}],
        )

    assert actual is response
    event = storage.query_events(task_id=str(task.task_id))[0]
    expected = tracker._pricing.get_cost(
        model,
        input_tokens=10,
        output_tokens=3,
        cached_tokens=4,
        cache_creation_tokens=2,
    )
    assert event.provider == "aws_bedrock"
    assert event.model == model
    assert event.input_tokens == 10
    assert event.output_tokens == 3
    assert event.cached_tokens == 4
    assert event.cost_usd == expected.cost_usd == Decimal("0.000153450")
    assert event.cost_confidence == "computed"
    assert event.details["cache_creation_input_tokens"] == 2
    assert event.details["attribution_operation_name"] == "bedrock.converse"
    assert event.details["provider_record_id"] == "converse-current-1"
    assert event.details["provider_retry_count"] == 2
    dimensions = {
        item["key"]: item["value"]["value"]
        for item in event.details["attribution_dimensions"]
    }
    assert dimensions == {"inference_latency": "standard", "service_tier": "default"}
    usage = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert usage == {
        "input_tokens": "10",
        "output_tokens": "3",
        "cache_read_input_tokens": "4",
        "cache_write_input_tokens": "2",
        "tool_call_count": "1",
    }
    persisted = json.dumps(event.to_dict())
    assert private_prompt not in persisted
    assert private_tool_name not in persisted
    assert "private-tool-id" not in persisted


def test_current_converse_stream_uses_terminal_metadata_and_tool_start(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)
    events = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {
                    "toolUse": {
                        "toolUseId": "private-stream-tool-id",
                        "name": "private_stream_tool",
                    }
                },
            }
        },
        {
            "metadata": {
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 5,
                    "totalTokens": 24,
                    "cacheReadInputTokens": 4,
                    "cacheWriteInputTokens": 3,
                    "cacheDetails": [
                        {"ttl": "1h", "inputTokens": 2},
                        {"ttl": "5m", "inputTokens": 1},
                    ],
                },
                "metrics": {"latencyMs": 2},
            }
        },
    ]
    response = {
        "stream": iter(events),
        "ResponseMetadata": {"RequestId": "converse-stream-current-1", "RetryAttempts": 1},
    }

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        assert operation_name == "ConverseStream"
        return response

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    with tracker.task(task_type="bedrock_current_converse_stream") as task:
        model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        actual = client.converse_stream(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": "private-stream-prompt"}]}],
        )
        assert list(actual["stream"]) == events

    event = storage.query_events(task_id=str(task.task_id))[0]
    assert event.input_tokens == 12
    assert event.output_tokens == 5
    assert event.cached_tokens == 4
    assert event.details["cache_creation_input_tokens"] == 3
    assert event.details["cache_creation_input_tokens_5m"] == 1
    assert event.details["cache_creation_input_tokens_1h"] == 2
    expected = tracker._pricing.get_cost(
        model,
        input_tokens=12,
        output_tokens=5,
        cached_tokens=4,
        cache_creation_tokens=1,
        cache_creation_tokens_1h=2,
    )
    assert event.cost_usd == expected.cost_usd == Decimal("0.000130950")
    assert event.cost_confidence == "computed"
    assert event.details["attribution_operation_name"] == "bedrock.converse_stream"
    assert event.details["provider_record_id"] == "converse-stream-current-1"
    usage = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert usage["tool_call_count"] == "1"
    persisted = json.dumps(event.to_dict())
    assert "private-stream-prompt" not in persisted
    assert "private_stream_tool" not in persisted
    assert "private-stream-tool-id" not in persisted


def test_bedrock_model_arn_identity_omits_account_ids() -> None:
    from dexcost.instruments.bedrock import _canonical_model

    assert (
        _canonical_model(
            "arn:aws:bedrock:us-east-1::foundation-model/"
            "anthropic.claude-opus-4-6-v1"
        )
        == "anthropic.claude-opus-4-6-v1"
    )
    application_arn = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/private-profile-id"
    )
    normalized = _canonical_model(application_arn)
    assert normalized == "bedrock-application-inference-profile"
    assert "123456789012" not in normalized
    assert "private-profile-id" not in normalized


def test_current_converse_attributes_router_guardrail_and_nonstandard_tier(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)
    private_account = "123456789012"
    private_router = "private-router-id"
    invoked_model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "private"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 8, "outputTokens": 2, "totalTokens": 10},
        "metrics": {"latencyMs": 1},
        "trace": {
            "promptRouter": {
                "invokedModelId": (
                    "arn:aws:bedrock:us-east-1::foundation-model/" + invoked_model
                )
            },
            "guardrail": {
                "inputAssessment": {
                    "opaque-assessment-id": {
                        "invocationMetrics": {
                            "usage": {
                                "contentPolicyUnits": 2,
                                "sensitiveInformationPolicyFreeUnits": 1,
                            }
                        }
                    }
                }
            },
        },
        "performanceConfig": {"latency": "optimized"},
        "serviceTier": {"type": "priority"},
        "ResponseMetadata": {"RequestId": "converse-router-1", "RetryAttempts": 0},
    }

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        assert operation_name == "Converse"
        return response

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    router_arn = (
        f"arn:aws:bedrock:us-east-1:{private_account}:"
        f"prompt-router/{private_router}"
    )
    with tracker.task(task_type="bedrock_current_router") as task:
        client.converse(
            modelId=router_arn,
            messages=[{"role": "user", "content": [{"text": "private-router-prompt"}]}],
        )

    event = storage.query_events(task_id=str(task.task_id))[0]
    assert event.model == invoked_model
    assert event.cost_usd > Decimal(0)
    assert event.cost_confidence == "unknown"
    usage = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert usage["intelligent_prompt_routing_requests"] == "1"
    assert usage["guardrail_content_policy_units"] == "2"
    assert usage["guardrail_sensitive_information_policy_free_units"] == "1"
    assert set(event.details["pricing_unpriced_dimensions"]) == {
        "guardrail_content_policy_units",
        "inference_latency",
        "intelligent_prompt_routing_requests",
        "service_tier",
    }
    dimensions = {
        item["key"]: item["value"]["value"]
        for item in event.details["attribution_dimensions"]
    }
    assert dimensions == {
        "inference_latency": "optimized",
        "prompt_router_used": "true",
        "service_tier": "priority",
    }
    persisted = json.dumps(event.to_dict())
    assert private_account not in persisted
    assert private_router not in persisted
    assert "private-router-prompt" not in persisted
    assert "opaque-assessment-id" not in persisted


def test_current_converse_stream_failure_and_close_record_once(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)

    def failing_stream() -> Iterator[dict[str, Any]]:
        yield {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {
                    "toolUse": {
                        "toolUseId": "private-failing-tool-id",
                        "name": "private_failing_tool",
                    }
                },
            }
        }
        raise RuntimeError("private-converse-stream-failure")

    responses = [
        {
            "stream": failing_stream(),
            "ResponseMetadata": {"RequestId": "current-stream-failed", "RetryAttempts": 3},
        },
        {
            "stream": iter([{"messageStart": {"role": "assistant"}}]),
            "ResponseMetadata": {
                "RequestId": "current-stream-cancelled",
                "RetryAttempts": 0,
            },
        },
    ]

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        assert operation_name == "ConverseStream"
        return responses.pop(0)

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    call = {
        "modelId": "anthropic.claude-opus-4-6-v1",
        "messages": [{"role": "user", "content": [{"text": "private-stream-input"}]}],
    }
    with tracker.task(task_type="bedrock_current_stream_lifecycle") as task:
        failed = client.converse_stream(**call)
        with pytest.raises(RuntimeError, match="private-converse-stream-failure"):
            list(failed["stream"])
        cancelled = client.converse_stream(**call)
        next(cancelled["stream"])
        cancelled["stream"].close()
        cancelled["stream"].close()

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    by_status = {
        event.details["attribution_operation_status"]: event for event in events
    }
    failed_event = by_status["failed"]
    assert failed_event.details["attribution_operation_name"] == "bedrock.converse_stream"
    assert failed_event.details["provider_record_id"] == "current-stream-failed"
    assert failed_event.details["provider_retry_count"] == 3
    failed_usage = {
        line["metric"]: line["quantity"]
        for line in failed_event.details["attribution_usage_lines"]
    }
    assert failed_usage == {"tool_call_count": "1"}
    cancelled_event = by_status["cancelled"]
    assert cancelled_event.details["attribution_operation_name"] == (
        "bedrock.converse_stream"
    )
    assert cancelled_event.details["provider_record_id"] == "current-stream-cancelled"
    persisted = json.dumps([event.to_dict() for event in events])
    assert "private-converse-stream-failure" not in persisted
    assert "private-stream-input" not in persisted
    assert "private_failing_tool" not in persisted
    assert "private-failing-tool-id" not in persisted


def test_current_invoke_model_classifies_embedding_image_and_rerank(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)

    def encoded(body: dict[str, Any], request_id: str) -> dict[str, Any]:
        return {
            "body": io.BytesIO(json.dumps(body).encode("utf-8")),
            "ResponseMetadata": {"RequestId": request_id, "RetryAttempts": 1},
        }

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        assert operation_name == "InvokeModel"
        model = api_params["modelId"]
        if model == "amazon.titan-embed-text-v2:0":
            return encoded(
                {"embedding": [0.1, 0.2], "inputTextTokenCount": 25},
                "bedrock-embedding-1",
            )
        if model == "amazon.titan-image-generator-v2:0":
            return encoded(
                {"images": ["private-image-one", "private-image-two"]},
                "bedrock-image-1",
            )
        if model == "amazon.rerank-v1:0":
            return encoded(
                {"results": [{"index": 0, "relevanceScore": 0.9}]},
                "bedrock-rerank-1",
            )
        raise AssertionError(f"unexpected model {model}")

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    private_prompt = "private-metered-bedrock-input"
    with tracker.task(task_type="bedrock_current_metered") as task:
        client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            body=json.dumps({"inputText": private_prompt}).encode(),
        )
        client.invoke_model(
            modelId="amazon.titan-image-generator-v2:0",
            body=json.dumps(
                {
                    "taskType": "TEXT_IMAGE",
                    "textToImageParams": {"text": private_prompt},
                    "imageGenerationConfig": {
                        "numberOfImages": 2,
                        "width": 2048,
                        "height": 2048,
                        "quality": "premium",
                    },
                }
            ).encode(),
        )
        client.invoke_model(
            modelId="amazon.rerank-v1:0",
            body=json.dumps(
                {"query": private_prompt, "documents": [{"text": "private-document"}]}
            ).encode(),
        )

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 3
    by_model = {event.model: event for event in events}

    embedding = by_model["amazon.titan-embed-text-v2:0"]
    assert embedding.event_type == "external_cost"
    assert embedding.input_tokens == 25
    assert embedding.cost_usd == Decimal("0.0000050")
    assert embedding.cost_confidence == "computed"
    assert embedding.details["provider_record_id"] == "bedrock-embedding-1"

    image = by_model["amazon.titan-image-generator-v2:0"]
    assert image.cost_usd == Decimal("0.024")
    assert image.cost_confidence == "computed"
    image_usage = {
        line["metric"]: line["quantity"]
        for line in image.details["attribution_usage_lines"]
    }
    assert image_usage == {"output_image_count": "2"}
    image_dimensions = {
        item["key"]: item["value"]["value"]
        for item in image.details["attribution_dimensions"]
    }
    assert image_dimensions == {
        "image_height": "2048",
        "image_quality": "premium",
        "image_width": "2048",
    }

    rerank = by_model["amazon.rerank-v1:0"]
    assert rerank.cost_usd == Decimal("0.001")
    assert rerank.cost_confidence == "computed"
    rerank_usage = {
        line["metric"]: line["quantity"]
        for line in rerank.details["attribution_usage_lines"]
    }
    assert rerank_usage == {"query_count": "1"}
    persisted = json.dumps([event.to_dict() for event in events])
    assert private_prompt not in persisted
    assert "private-document" not in persisted
    assert "private-image-one" not in persisted
    assert "private-image-two" not in persisted


def test_current_guardrail_count_tokens_and_async_job_lifecycle(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boto3, base_client = _load_current_boto3()
    client = _client(boto3)
    private_account = "123456789012"
    private_job = "private-async-invoke-id"
    invocation_arn = (
        f"arn:aws:bedrock:us-east-1:{private_account}:async-invoke/{private_job}"
    )
    get_statuses = ["InProgress", "Completed"]

    def fake_make_api_call(
        self: Any, operation_name: str, api_params: dict[str, Any]
    ) -> dict[str, Any]:
        if operation_name == "ApplyGuardrail":
            return {
                "usage": {
                    "contentPolicyUnits": 2,
                    "sensitiveInformationPolicyFreeUnits": 1,
                },
                "action": "GUARDRAIL_INTERVENED",
                "outputs": [{"text": "private-guardrail-output"}],
                "ResponseMetadata": {"RequestId": "guardrail-current-1", "RetryAttempts": 1},
            }
        if operation_name == "CountTokens":
            return {
                "inputTokens": 42,
                "ResponseMetadata": {"RequestId": "count-tokens-current-1", "RetryAttempts": 0},
            }
        if operation_name == "StartAsyncInvoke":
            return {
                "invocationArn": invocation_arn,
                "ResponseMetadata": {"RequestId": "async-start-current-1", "RetryAttempts": 0},
            }
        if operation_name == "GetAsyncInvoke":
            return {
                "invocationArn": invocation_arn,
                "modelArn": (
                    "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-reel-v1:1"
                ),
                "status": get_statuses.pop(0),
                "outputDataConfig": {
                    "s3OutputDataConfig": {"s3Uri": "s3://private-bucket/private-output"}
                },
                "ResponseMetadata": {"RequestId": "async-get-current-1", "RetryAttempts": 0},
            }
        raise AssertionError(f"unexpected operation {operation_name}")

    monkeypatch.setattr(base_client, "_make_api_call", fake_make_api_call)
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    with tracker.task(task_type="bedrock_current_auxiliary") as task:
        client.apply_guardrail(
            guardrailIdentifier="private-guardrail-id",
            guardrailVersion="1",
            source="INPUT",
            content=[{"text": {"text": "private-guardrail-input"}}],
        )
        client.count_tokens(
            modelId="anthropic.claude-opus-4-6-v1",
            input={
                "converse": {
                    "messages": [
                        {"role": "user", "content": [{"text": "private-count-input"}]}
                    ]
                }
            },
        )
        client.start_async_invoke(
            modelId="amazon.nova-reel-v1:1",
            modelInput={"taskType": "TEXT_VIDEO", "textToVideoParams": {"text": "private"}},
            outputDataConfig={
                "s3OutputDataConfig": {"s3Uri": "s3://private-bucket/private-output"}
            },
        )

    client.get_async_invoke(invocationArn=invocation_arn)
    client.get_async_invoke(invocationArn=invocation_arn)

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    by_operation = {
        event.details["attribution_operation_name"]: event for event in events
    }
    guardrail = by_operation["bedrock.apply_guardrail"]
    assert guardrail.cost_confidence == "unknown"
    assert guardrail.details["provider_record_id"] == "guardrail-current-1"
    guardrail_usage = {
        line["metric"]: line["quantity"]
        for line in guardrail.details["attribution_usage_lines"]
    }
    assert guardrail_usage == {
        "guardrail_content_policy_units": "2",
        "guardrail_sensitive_information_policy_free_units": "1",
    }
    count_tokens = by_operation["bedrock.count_tokens"]
    assert count_tokens.cost_usd == Decimal(0)
    assert count_tokens.cost_confidence == "unknown"
    assert count_tokens.details["attribution_usage_lines"] == [
        {"metric": "input_tokens", "quantity": "42", "unit": "Tokens"}
    ]

    record_id = hashlib.sha256(invocation_arn.encode()).hexdigest()
    latest = storage.get_provider_job(
        "aws_bedrock", "bedrock_async_invoke", record_id
    )
    assert latest is not None
    history = storage.query_provider_job_history(str(latest.event_id))
    assert [revision.status for revision in history] == [
        "submitted",
        "running",
        "succeeded",
    ]
    assert history[-1].cost_amount is None
    assert [(line.metric, str(line.quantity)) for line in history[-1].usage] == [
        ("request_count", "1")
    ]
    assert latest.resource_id == "amazon.nova-reel-v1:1"
    persisted = json.dumps(
        [event.to_dict() for event in events]
        + [revision.to_dict() for revision in history]
    )
    for private in (
        private_account,
        private_job,
        "private-guardrail-id",
        "private-guardrail-input",
        "private-guardrail-output",
        "private-count-input",
        "private-bucket",
        "private-output",
    ):
        assert private not in persisted


@pytest.mark.asyncio
async def test_current_smithy_nova_sonic_bidirectional_usage(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_module = pytest.importorskip("aws_sdk_bedrock_runtime.client")
    model_module = pytest.importorskip("aws_sdk_bedrock_runtime.models")
    client_type = client_module.BedrockRuntimeClient

    class InputStream:
        async def close(self) -> None:
            return None

    class Receiver:
        def __init__(self, items: list[Any]) -> None:
            self.items = items

        async def receive(self) -> Any:
            return self.items.pop(0) if self.items else None

        async def close(self) -> None:
            return None

    class Duplex:
        def __init__(self, receiver: Receiver) -> None:
            self.input_stream = InputStream()
            self.receiver = receiver

        async def await_output(self) -> tuple[Any, Receiver]:
            return model_module.InvokeModelWithBidirectionalStreamOperationOutput(), self.receiver

        async def close(self) -> None:
            await self.input_stream.close()
            await self.receiver.close()

    def output(payload: dict[str, Any]) -> Any:
        return model_module.InvokeModelWithBidirectionalStreamOutputChunk(
            model_module.BidirectionalOutputPayloadPart(
                bytes_=json.dumps(payload).encode("utf-8")
            )
        )

    receiver = Receiver(
        [
            output(
                {
                    "event": {
                        "usageEvent": {
                            "completionId": "private-completion-id",
                            "details": {
                                "total": {
                                    "input": {"speechTokens": 10, "textTokens": 2},
                                    "output": {"speechTokens": 8, "textTokens": 3},
                                }
                            },
                        }
                    }
                }
            ),
            output(
                {
                    "event": {
                        "toolUse": {
                            "toolUseId": "private-sonic-tool-id",
                            "toolName": "private_sonic_tool",
                            "content": "private-sonic-tool-input",
                        }
                    }
                }
            ),
        ]
    )
    duplex = Duplex(receiver)

    async def fake_bidi(self: Any, input: Any, plugins: Any = None) -> Duplex:
        return duplex

    monkeypatch.setattr(
        client_type, "invoke_model_with_bidirectional_stream", fake_bidi
    )
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    client = object.__new__(client_type)
    operation_input = model_module.InvokeModelWithBidirectionalStreamOperationInput(
        model_id="amazon.nova-2-sonic-v1:0"
    )
    with tracker.task(task_type="bedrock_current_sonic") as task:
        stream = await client.invoke_model_with_bidirectional_stream(
            input=operation_input
        )
        initial, output_stream = await stream.await_output()
        assert isinstance(
            initial, model_module.InvokeModelWithBidirectionalStreamOperationOutput
        )
        assert await output_stream.receive() is not None
        assert await output_stream.receive() is not None
        assert await output_stream.receive() is None

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    event = events[0]
    assert event.model == "amazon.nova-2-sonic-v1:0"
    assert event.details["attribution_operation_name"] == (
        "bedrock.invoke_model_bidirectional_stream"
    )
    assert event.details["attribution_operation_status"] == "succeeded"
    assert event.input_tokens == 12
    assert event.output_tokens == 11
    assert event.cost_confidence == "unknown"
    usage = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert usage == {
        "input_audio_tokens": "10",
        "input_tokens": "2",
        "output_audio_tokens": "8",
        "output_tokens": "3",
        "tool_call_count": "1",
    }
    persisted = json.dumps(event.to_dict())
    assert "private-completion-id" not in persisted
    assert "private-sonic-tool-id" not in persisted
    assert "private_sonic_tool" not in persisted
    assert "private-sonic-tool-input" not in persisted


@pytest.mark.asyncio
async def test_current_smithy_nova_sonic_cancel_and_failure_are_partial_once(
    tracker: CostTracker,
    storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_module = pytest.importorskip("aws_sdk_bedrock_runtime.client")
    model_module = pytest.importorskip("aws_sdk_bedrock_runtime.models")
    client_type = client_module.BedrockRuntimeClient

    usage_payload = json.dumps(
        {
            "event": {
                "usageEvent": {
                    "completionId": "private-partial-completion",
                    "details": {
                        "total": {
                            "input": {"speechTokens": 4, "textTokens": 1},
                            "output": {"speechTokens": 2, "textTokens": 1},
                        }
                    },
                }
            }
        }
    ).encode()
    usage_item = SimpleNamespace(value=SimpleNamespace(bytes_=usage_payload))

    class InputStream:
        async def close(self) -> None:
            return None

    class Receiver:
        def __init__(self, *, fail: bool) -> None:
            self.first = True
            self.fail = fail

        async def receive(self) -> Any:
            if self.first:
                self.first = False
                return usage_item
            if self.fail:
                raise RuntimeError("private-sonic-receiver-failure")
            return None

        async def close(self) -> None:
            return None

    class Duplex:
        def __init__(self, receiver: Receiver) -> None:
            self.input_stream = InputStream()
            self.receiver = receiver

        async def await_output(self) -> tuple[Any, Receiver]:
            return model_module.InvokeModelWithBidirectionalStreamOperationOutput(), self.receiver

        async def close(self) -> None:
            await self.input_stream.close()
            await self.receiver.close()

    streams = [Duplex(Receiver(fail=False)), Duplex(Receiver(fail=True))]

    async def fake_bidi(self: Any, input: Any, plugins: Any = None) -> Duplex:
        return streams.pop(0)

    monkeypatch.setattr(
        client_type, "invoke_model_with_bidirectional_stream", fake_bidi
    )
    from dexcost.instruments.bedrock import instrument_bedrock

    instrument_bedrock(tracker)
    client = object.__new__(client_type)
    operation_input = model_module.InvokeModelWithBidirectionalStreamOperationInput(
        model_id="amazon.nova-2-sonic-v1:0"
    )
    with tracker.task(task_type="bedrock_current_sonic_lifecycle") as task:
        cancelled = await client.invoke_model_with_bidirectional_stream(
            input=operation_input
        )
        _, cancelled_output = await cancelled.await_output()
        assert await cancelled_output.receive() is usage_item
        await cancelled_output.close()
        await cancelled_output.close()

        failed = await client.invoke_model_with_bidirectional_stream(input=operation_input)
        _, failed_output = await failed.await_output()
        assert await failed_output.receive() is usage_item
        with pytest.raises(RuntimeError, match="private-sonic-receiver-failure"):
            await failed_output.receive()

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    by_status = {
        event.details["attribution_operation_status"]: event for event in events
    }
    assert set(by_status) == {"cancelled", "failed"}
    for event in events:
        assert event.input_tokens == 5
        assert event.output_tokens == 3
    persisted = json.dumps([event.to_dict() for event in events])
    assert "private-partial-completion" not in persisted
    assert "private-sonic-receiver-failure" not in persisted
