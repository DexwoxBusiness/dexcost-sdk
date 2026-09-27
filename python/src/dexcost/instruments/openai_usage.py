"""Normalize OpenAI token usage into mutually exclusive billing buckets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenAIUsage:
    """Provider totals plus the five disjoint DexCost billing quantities."""

    total_input_tokens: int
    input_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    total_output_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


class OpenAIUsageError(ValueError):
    """Raised when provider usage cannot be represented without guessing."""


def _field(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        # Pydantic models, dataclasses, SimpleNamespace, and the SDK
        # compatibility mocks all expose assigned fields here. Pydantic v2
        # keeps provider extensions in ``__pydantic_extra__``/``model_extra``
        # instead, so inspect those mappings before failing closed. This is
        # required for gateway fields such as LiteLLM ``cache_write_tokens``
        # and Perplexity ``cache_creation_input_tokens`` that are not declared
        # by the base OpenAI model.
        if key in attributes:
            return attributes[key]
        for extras_name in ("__pydantic_extra__", "model_extra"):
            extras = getattr(value, extras_name, None)
            if isinstance(extras, Mapping) and key in extras:
                return extras[key]
        # Avoid treating a dynamically-created mock child as real usage.
        return None
    return getattr(value, key, None)


def _counter(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_counter(value: object) -> int:
    if value is None:
        return 0
    parsed = _counter(value)
    if parsed is None:
        raise OpenAIUsageError("token counters must be non-negative integers")
    return parsed


def normalize_openai_usage(usage: object) -> OpenAIUsage:
    """Normalize Chat Completions or Responses API usage.

    OpenAI reports cache reads and writes inside the total input count, and
    reasoning tokens inside the total output count.  DexCost emits each bucket
    exactly once, so impossible overlaps fail closed instead of being clamped.
    """

    chat_input = _field(usage, "prompt_tokens")
    responses_input = _field(usage, "input_tokens")
    chat_output = _field(usage, "completion_tokens")
    responses_output = _field(usage, "output_tokens")

    total_input = _counter(chat_input if chat_input is not None else responses_input)
    total_output = _counter(chat_output if chat_output is not None else responses_output)
    if total_input is None or total_output is None:
        if (chat_input is not None or responses_input is not None) and (
            chat_output is not None or responses_output is not None
        ):
            raise OpenAIUsageError("token counters must be non-negative integers")
        raise OpenAIUsageError("usage is missing input or output token totals")

    input_details = _field(usage, "prompt_tokens_details")
    if input_details is None:
        input_details = _field(usage, "input_tokens_details")
    output_details = _field(usage, "completion_tokens_details")
    if output_details is None:
        output_details = _field(usage, "output_tokens_details")

    cached_value = (
        _field(input_details, "cached_tokens")
        if input_details is not None
        else _field(usage, "cached_tokens")
    )
    if cached_value is None and input_details is not None:
        # Perplexity's native Agent API uses this more explicit spelling.
        cached_value = _field(input_details, "cache_read_input_tokens")
    if cached_value is None:
        # DeepSeek's OpenAI-compatible Chat Completions response reports the
        # provider-billed cache bucket at the top level of ``usage``.
        cached_value = _field(usage, "prompt_cache_hit_tokens")
    cache_write_value = (
        _field(input_details, "cache_write_tokens") if input_details is not None else None
    )
    if cache_write_value is None and input_details is not None:
        cache_write_value = _field(input_details, "cache_creation_input_tokens")
    cached = _optional_counter(cached_value)
    cache_write = _optional_counter(cache_write_value)
    reasoning = _optional_counter(
        _field(output_details, "reasoning_tokens")
        if output_details is not None
        else _field(usage, "reasoning_tokens")
    )

    if cached + cache_write > total_input:
        raise OpenAIUsageError("cache token buckets exceed total input tokens")
    if reasoning > total_output:
        raise OpenAIUsageError("reasoning tokens exceed total output tokens")

    return OpenAIUsage(
        total_input_tokens=total_input,
        input_tokens=total_input - cached - cache_write,
        cache_read_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        total_output_tokens=total_output,
        output_tokens=total_output - reasoning,
        reasoning_output_tokens=reasoning,
    )
