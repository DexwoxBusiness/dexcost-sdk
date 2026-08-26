"""LLM pricing engine — automatic cost calculation from model name and token counts.

Implements US-010: bundles LiteLLM's model_cost_map.json, resolves model aliases,
supports custom pricing, and provides background pricing data updates.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from dexcost._user_agent import sdk_user_agent

logger = logging.getLogger(__name__)


def _uses_disjoint_cache_buckets(model: str, provider: str = "") -> bool:
    """Return whether usage exposes cache tokens outside ``input_tokens``."""
    normalized_model = model.lower()
    normalized_provider = provider.lower()
    return (
        normalized_provider
        in {
            "anthropic",
            "bedrock",
            "bedrock_converse",
            "vertex_ai-anthropic_models",
        }
        or "claude" in normalized_model
        or "anthropic." in normalized_model
    )


@dataclass(frozen=True)
class CostResult:
    """Result of a cost calculation.

    Attributes:
        cost_usd: Calculated cost in USD.
        cost_confidence: How trustworthy the cost is (``"computed"`` or ``"unknown"``).
        pricing_source: Where pricing data came from (``"litellm"``, ``"custom"``, ``"unknown"``).
        pricing_version: Hash of the pricing data used, for reproducibility.
    """

    cost_usd: Decimal
    cost_confidence: str
    pricing_source: str
    pricing_version: str


@dataclass(frozen=True)
class MeteredCostLine:
    """One exact catalog-rate multiplication used for multimodal pricing."""

    dimension: str
    quantity: Decimal
    rate_field: str
    rate_usd: Decimal
    cost_usd: Decimal


@dataclass(frozen=True)
class MeteredCostResult:
    """A catalog-backed cost for arbitrary provider-reported usage meters.

    ``unpriced_dimensions`` is deliberately explicit.  A partially priced
    operation retains the known line costs but receives ``unknown`` confidence
    so callers never mistake a lower bound for a complete charge.
    """

    cost_usd: Decimal
    cost_confidence: str
    pricing_source: str
    pricing_version: str
    resolved_model: str | None
    lines: tuple[MeteredCostLine, ...]
    unpriced_dimensions: tuple[str, ...]


# Canonical provider meters mapped to fields in the authoritative model-price
# catalog.  Alternatives are ordered: the first field present on a model wins.
# The third tuple item is a divisor for catalog rates expressed per N units.
_METERED_RATE_FIELDS: dict[str, tuple[tuple[str, Decimal], ...]] = {
    "input_tokens": (("input_cost_per_token", Decimal(1)),),
    "output_tokens": (("output_cost_per_token", Decimal(1)),),
    "cache_read_input_tokens": (("cache_read_input_token_cost", Decimal(1)),),
    "cache_write_input_tokens": (("cache_creation_input_token_cost", Decimal(1)),),
    "cache_write_input_tokens_1h": (
        ("cache_creation_input_token_cost_above_1hr", Decimal(1)),
    ),
    "reasoning_output_tokens": (
        ("output_cost_per_reasoning_token", Decimal(1)),
        ("output_cost_per_token", Decimal(1)),
    ),
    "input_image_tokens": (
        ("input_cost_per_image_token", Decimal(1)),
        ("input_cost_per_token", Decimal(1)),
    ),
    "cache_read_input_image_tokens": (
        ("cache_read_input_image_token_cost", Decimal(1)),
        ("cache_read_input_token_cost", Decimal(1)),
    ),
    "output_image_tokens": (("output_cost_per_image_token", Decimal(1)),),
    "input_audio_tokens": (("input_cost_per_audio_token", Decimal(1)),),
    "cache_read_input_audio_tokens": (
        ("cache_read_input_audio_token_cost", Decimal(1)),
    ),
    "cache_write_input_audio_tokens": (
        ("cache_creation_input_audio_token_cost", Decimal(1)),
    ),
    "output_audio_tokens": (("output_cost_per_audio_token", Decimal(1)),),
    "input_video_tokens": (
        ("input_cost_per_video_token", Decimal(1)),
        ("input_cost_per_token", Decimal(1)),
    ),
    "cache_read_input_video_tokens": (
        ("cache_read_input_video_token_cost", Decimal(1)),
        ("cache_read_input_token_cost", Decimal(1)),
    ),
    "output_video_tokens": (
        ("output_cost_per_video_token", Decimal(1)),
        ("output_cost_per_token", Decimal(1)),
    ),
    "tool_input_tokens": (("input_cost_per_token", Decimal(1)),),
    "tool_input_image_tokens": (
        ("input_cost_per_image_token", Decimal(1)),
        ("input_cost_per_token", Decimal(1)),
    ),
    "tool_input_audio_tokens": (("input_cost_per_audio_token", Decimal(1)),),
    "tool_input_video_tokens": (
        ("input_cost_per_video_token", Decimal(1)),
        ("input_cost_per_token", Decimal(1)),
    ),
    "characters": (("input_cost_per_character", Decimal(1)),),
    "output_characters": (("output_cost_per_character", Decimal(1)),),
    "input_audio_seconds": (
        ("input_cost_per_audio_per_second", Decimal(1)),
        ("input_cost_per_second", Decimal(1)),
    ),
    "output_audio_seconds": (("output_cost_per_second", Decimal(1)),),
    "input_video_seconds": (("input_cost_per_video_per_second", Decimal(1)),),
    "output_video_seconds": (
        ("output_cost_per_video_per_second", Decimal(1)),
        ("output_cost_per_second", Decimal(1)),
    ),
    "image_count": (("input_cost_per_image", Decimal(1)),),
    "output_image_count": (("output_cost_per_image", Decimal(1)),),
    "output_image_count_premium": (
        ("output_cost_per_image_premium_image", Decimal(1)),
    ),
    "output_image_count_above_1024": (
        ("output_cost_per_image_above_1024_and_1024_pixels", Decimal(1)),
    ),
    "output_image_count_above_1024_premium": (
        (
            "output_cost_per_image_above_1024_and_1024_pixels_and_premium_image",
            Decimal(1),
        ),
    ),
    "input_pixels": (("input_cost_per_pixel", Decimal(1)),),
    "output_pixels": (("output_cost_per_pixel", Decimal(1)),),
    "request_count": (("input_cost_per_request", Decimal(1)),),
    "query_count": (("input_cost_per_query", Decimal(1)),),
    "web_search_calls": (
        ("web_search_cost_per_call", Decimal(1)),
        ("input_cost_per_query", Decimal(1)),
        ("search_context_cost_per_query", Decimal(1)),
    ),
    "session_count": (("code_interpreter_cost_per_session", Decimal(1)),),
    "file_search_calls": (("file_search_cost_per_1k_calls", Decimal(1000)),),
    # Provider batch APIs publish separate discounted token rates. Never fall
    # back to synchronous rates: a missing batch field is unknown pricing, not
    # evidence that the ordinary rate applies.
    "batch_input_tokens": (("input_cost_per_token_batches", Decimal(1)),),
    "batch_output_tokens": (("output_cost_per_token_batches", Decimal(1)),),
    "batch_reasoning_output_tokens": (
        ("output_cost_per_token_batches", Decimal(1)),
    ),
    # Anthropic's Message Batches discount is 50% and stacks with prompt
    # caching. Provider-specific dimensions make that documented policy
    # explicit without changing the meaning of generic batch meters.
    "anthropic_batch_input_tokens": (("input_cost_per_token", Decimal(2)),),
    "anthropic_batch_output_tokens": (("output_cost_per_token", Decimal(2)),),
    "anthropic_batch_cache_read_input_tokens": (
        ("cache_read_input_token_cost", Decimal(2)),
    ),
    "anthropic_batch_cache_write_input_tokens": (
        ("cache_creation_input_token_cost", Decimal(2)),
    ),
    "anthropic_batch_cache_write_input_tokens_1h": (
        ("cache_creation_input_token_cost_above_1hr", Decimal(2)),
    ),
}


def _metered_quantity(name: str, value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(
            f"metered usage {name!r} must be an integer, Decimal, or decimal string"
        )
    try:
        quantity = value if isinstance(value, Decimal) else Decimal(str(value))
    except (decimal.InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"metered usage {name!r} is not a plain decimal") from exc
    if not quantity.is_finite() or quantity < 0:
        raise ValueError(f"metered usage {name!r} must be finite and non-negative")
    return quantity


def _metered_rate(model_info: Mapping[str, object], rate_field: str) -> Decimal:
    """Read a scalar catalog rate, including unambiguous search-rate maps."""
    raw_rate = model_info[rate_field]
    if rate_field == "search_context_cost_per_query" and isinstance(raw_rate, Mapping):
        # LiteLLM represents some provider search prices as a context-size map.
        # Anthropic reports only a search count (no context size), so it is safe
        # to use the map only when every published tier has the same price.
        rates = tuple(Decimal(str(value)) for value in raw_rate.values())
        if not rates or any(rate != rates[0] for rate in rates[1:]):
            raise ValueError("search context rates are not a single flat provider rate")
        rate = rates[0]
    else:
        rate = Decimal(str(raw_rate))
    if not rate.is_finite() or rate < 0:
        raise ValueError("catalog rate must be finite and non-negative")
    return rate


@dataclass
class CustomPricing:
    """Per-model custom pricing override.

    Rates are expressed per 1 000 tokens.
    """

    input_per_1k: Decimal
    output_per_1k: Decimal


class PricingEngine:
    """Calculate LLM costs from model name and token counts.

    Loads pricing data from the bundled LiteLLM ``model_cost_map.json`` on
    construction.  Custom per-model pricing can be registered via
    :meth:`set_custom_pricing`.

    Args:
        data_path: Optional path to a ``model_cost_map.json`` file.  When
            ``None``, the bundled copy inside the package is used.
        auto_update: If ``True``, a background thread will periodically
            check for updated pricing data.  Defaults to ``False`` in v1.0
            (PRD: no background update — bundled map + manual override only).
    """

    _UPDATE_URL = (
        "https://raw.githubusercontent.com/BerriAI/litellm/"
        "main/model_prices_and_context_window.json"
    )
    _UPDATE_INTERVAL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        data_path: str | Path | None = None,
        *,
        auto_update: bool = False,
        api_key: str | None = None,
        catalog_data: dict[str, dict[str, Any]] | None = None,
        catalog_version: str | None = None,
    ) -> None:
        self._custom_pricing: dict[str, CustomPricing] = {}
        self._lock = threading.Lock()
        self._api_key: str | None = api_key

        # Load bundled pricing data
        if data_path is not None and catalog_data is not None:
            raise ValueError("data_path and catalog_data are mutually exclusive")
        if catalog_data is not None:
            raw = json.dumps(catalog_data, sort_keys=True, separators=(",", ":"))
        elif data_path is not None:
            raw = Path(data_path).read_text(encoding="utf-8")
        else:
            raw = _read_bundled_data()

        try:
            self._model_map: dict[str, dict[str, Any]] = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to load pricing data: %s", exc)
            self._model_map = {}
        # Remove the spec entry — not a real model
        self._model_map.pop("sample_spec", None)
        self._pricing_version = catalog_version or _compute_hash(raw)

        # Background updater
        self._update_timer: threading.Timer | None = None
        self._server_refresh_stop = threading.Event()
        self._server_refresh_thread: threading.Thread | None = None
        if auto_update:
            self._schedule_update()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_creation_tokens_1h: int = 0,
    ) -> CostResult:
        """Calculate the cost for an LLM call.

        Args:
            model: Model identifier (e.g. ``"gpt-4o"``).
            input_tokens: Number of input (prompt) tokens.
            output_tokens: Number of output (completion) tokens.
            cached_tokens: Number of cached input tokens that receive a
                discount. OpenAI includes these inside ``input_tokens``;
                Anthropic reports them as a separate, disjoint bucket.
            cache_creation_tokens: Number of input tokens written to cache
                with the default TTL (five minutes for Anthropic and
                Bedrock). Charged at the higher
                ``cache_creation_input_token_cost`` rate instead of the normal
                input rate.
            cache_creation_tokens_1h: Number of input tokens written with the
                one-hour TTL. Charged at the catalog's
                ``cache_creation_input_token_cost_above_1hr`` rate. This is a
                disjoint provider-reported bucket, not part of
                ``cache_creation_tokens``.

        Returns:
            A :class:`CostResult` with ``cost_usd``, ``cost_confidence``,
            ``pricing_source``, and ``pricing_version``.
        """
        # 1. Check custom pricing first
        with self._lock:
            custom = self._custom_pricing.get(model)
            model_info = None if custom is not None else self._resolve_model(model)
            pricing_version = self._pricing_version
        if custom is not None:
            safe_input = max(0, input_tokens)
            safe_output = max(0, output_tokens)
            safe_cached = max(0, cached_tokens)
            safe_creation = max(0, cache_creation_tokens)
            safe_creation_1h = max(0, cache_creation_tokens_1h)
            has_unpriced_disjoint_cache = _uses_disjoint_cache_buckets(model) and (
                safe_cached > 0 or safe_creation > 0 or safe_creation_1h > 0
            )
            billable_input = safe_input
            if has_unpriced_disjoint_cache:
                billable_input += safe_cached + safe_creation + safe_creation_1h
            cost = custom.input_per_1k * Decimal(str(billable_input)) / Decimal(
                "1000"
            ) + custom.output_per_1k * Decimal(str(safe_output)) / Decimal("1000")
            return CostResult(
                cost_usd=cost,
                cost_confidence=(
                    "unknown" if has_unpriced_disjoint_cache else "computed"
                ),
                pricing_source="custom",
                pricing_version=pricing_version,
            )

        # 2. Resolve from bundled LiteLLM data
        if model_info is None:
            logger.warning(
                "Model %r not found in pricing data; setting cost_usd=0 "
                "and cost_confidence='unknown'.",
                model,
            )
            return CostResult(
                cost_usd=Decimal("0"),
                cost_confidence="unknown",
                pricing_source="unknown",
                pricing_version=pricing_version,
            )

        # JSON stores prices as float literals (e.g. 0.0000025).  Python's
        # json.loads produces exact IEEE 754 doubles for these simple values
        # and str() renders them without precision loss, so Decimal(str(float))
        # is safe here.  Avoid arithmetic on the raw floats before conversion.
        input_cost_per_token = Decimal(str(model_info.get("input_cost_per_token", 0)))
        output_cost_per_token = Decimal(str(model_info.get("output_cost_per_token", 0)))
        has_cache_read_rate = "cache_read_input_token_cost" in model_info
        has_cache_creation_rate = "cache_creation_input_token_cost" in model_info
        has_cache_creation_1h_rate = (
            "cache_creation_input_token_cost_above_1hr" in model_info
        )
        cache_read_cost_per_token = Decimal(
            str(model_info.get("cache_read_input_token_cost", input_cost_per_token))
        )
        cache_creation_cost_per_token = Decimal(
            str(model_info.get("cache_creation_input_token_cost", input_cost_per_token))
        )
        cache_creation_1h_cost_per_token = Decimal(
            str(
                model_info.get(
                    "cache_creation_input_token_cost_above_1hr",
                    input_cost_per_token,
                )
            )
        )
        safe_input = max(0, input_tokens)
        safe_output = max(0, output_tokens)
        safe_cached = max(0, cached_tokens)
        safe_creation = max(0, cache_creation_tokens)
        safe_creation_1h = max(0, cache_creation_tokens_1h)
        disjoint_cache_buckets = _uses_disjoint_cache_buckets(
            model, str(model_info.get("litellm_provider", ""))
        )

        cost_confidence = "computed"
        if disjoint_cache_buckets:
            # Anthropic reports input, cache-read, and cache-write as separate
            # billable buckets. None may be clamped to or subtracted from another.
            cost = (
                input_cost_per_token * Decimal(str(safe_input))
                + cache_read_cost_per_token * Decimal(str(safe_cached))
                + cache_creation_cost_per_token * Decimal(str(safe_creation))
                + cache_creation_1h_cost_per_token
                * Decimal(str(safe_creation_1h))
                + output_cost_per_token * Decimal(str(safe_output))
            )
            if (safe_cached > 0 and not has_cache_read_rate) or (
                safe_creation > 0 and not has_cache_creation_rate
            ) or (
                safe_creation_1h > 0 and not has_cache_creation_1h_rate
            ):
                cost_confidence = "unknown"
        else:
            # OpenAI includes cached tokens in input_tokens. Only apply a
            # discount when the catalog explicitly supplies the cache rate.
            effective_cached = min(safe_cached, safe_input) if has_cache_read_rate else 0
            remaining = safe_input - effective_cached
            effective_creation = (
                min(safe_creation, remaining) if has_cache_creation_rate else 0
            )
            remaining -= effective_creation
            effective_creation_1h = (
                min(safe_creation_1h, remaining)
                if has_cache_creation_1h_rate
                else 0
            )
            non_cached_input = remaining - effective_creation_1h
            cost = (
                input_cost_per_token * Decimal(str(non_cached_input))
                + cache_read_cost_per_token * Decimal(str(effective_cached))
                + cache_creation_cost_per_token * Decimal(str(effective_creation))
                + cache_creation_1h_cost_per_token
                * Decimal(str(effective_creation_1h))
                + output_cost_per_token * Decimal(str(safe_output))
            )

        return CostResult(
            cost_usd=cost,
            cost_confidence=cost_confidence,
            pricing_source="litellm",
            pricing_version=pricing_version,
        )

    def get_metered_cost(
        self,
        model: str,
        usage: Mapping[str, Decimal | int | str],
        *,
        model_candidates: Sequence[str] = (),
    ) -> MeteredCostResult:
        """Price exact multimodal usage with the active model catalog.

        This is the non-chat companion to :meth:`get_cost`.  It handles the
        native quantities returned by embedding, image, audio, video, realtime,
        and provider-tool APIs without translating them into fictional text
        tokens.  Prompt/output values are never accepted here; only quantities
        and catalog identifiers participate in pricing.

        ``model_candidates`` supports documented pricing variants such as the
        high-resolution Sora SKU.  Candidates are tried in order before the
        provider-reported base model.
        """
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        if not isinstance(usage, Mapping):
            raise TypeError("usage must be a mapping")

        quantities: dict[str, Decimal] = {}
        for dimension, raw_quantity in usage.items():
            if not isinstance(dimension, str) or not dimension:
                raise ValueError("metered usage dimensions must be non-empty strings")
            quantities[dimension] = _metered_quantity(dimension, raw_quantity)

        ordered_candidates: list[str] = []
        for candidate in (*model_candidates, model):
            if not isinstance(candidate, str) or not candidate:
                raise ValueError("model candidates must be non-empty strings")
            if candidate not in ordered_candidates:
                ordered_candidates.append(candidate)

        with self._lock:
            resolved_model: str | None = None
            model_info: dict[str, Any] | None = None
            for candidate in ordered_candidates:
                resolved = self._resolve_model_entry(candidate)
                if resolved is not None:
                    resolved_model, model_info = resolved
                    break
            pricing_version = self._pricing_version

        positive = {name: value for name, value in quantities.items() if value > 0}
        if model_info is None or not positive:
            return MeteredCostResult(
                cost_usd=Decimal(0),
                cost_confidence="unknown",
                pricing_source="unknown" if model_info is None else "litellm",
                pricing_version=pricing_version,
                resolved_model=resolved_model,
                lines=(),
                unpriced_dimensions=tuple(sorted(positive)),
            )

        lines: list[MeteredCostLine] = []
        unpriced: list[str] = []
        for dimension, quantity in sorted(positive.items()):
            alternatives = _METERED_RATE_FIELDS.get(dimension)
            selected: tuple[str, Decimal] | None = None
            if alternatives is not None:
                selected = next(
                    (
                        (rate_field, divisor)
                        for rate_field, divisor in alternatives
                        if rate_field in model_info
                    ),
                    None,
                )
            if selected is None:
                unpriced.append(dimension)
                continue
            rate_field, divisor = selected
            try:
                rate = _metered_rate(model_info, rate_field)
            except (decimal.InvalidOperation, TypeError, ValueError):
                unpriced.append(dimension)
                continue
            line_cost = quantity * rate / divisor
            lines.append(
                MeteredCostLine(
                    dimension=dimension,
                    quantity=quantity,
                    rate_field=rate_field,
                    rate_usd=rate,
                    cost_usd=line_cost,
                )
            )

        return MeteredCostResult(
            cost_usd=sum((line.cost_usd for line in lines), Decimal(0)),
            cost_confidence="unknown" if unpriced else "computed",
            pricing_source="litellm",
            pricing_version=pricing_version,
            resolved_model=resolved_model,
            lines=tuple(lines),
            unpriced_dimensions=tuple(sorted(unpriced)),
        )

    def set_custom_pricing(
        self,
        model: str,
        input_per_1k: Decimal | str | float,
        output_per_1k: Decimal | str | float,
    ) -> None:
        """Register custom per-token pricing for a model.

        Custom pricing takes precedence over bundled LiteLLM data.

        Args:
            model: Model identifier (e.g. ``"ft:gpt-4o-my-finetune"``).
            input_per_1k: Cost per 1 000 input tokens.
            output_per_1k: Cost per 1 000 output tokens.
        """
        try:
            custom = CustomPricing(
                input_per_1k=Decimal(str(input_per_1k)),
                output_per_1k=Decimal(str(output_per_1k)),
            )
        except (decimal.InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid pricing values for {model}: {exc}") from exc
        with self._lock:
            self._custom_pricing[model] = custom

    def set_api_key(self, api_key: str | None) -> None:
        """Update authentication used by control-plane pricing refreshes."""
        with self._lock:
            self._api_key = api_key

    def replace_catalog(
        self,
        catalog_data: dict[str, dict[str, Any]],
        catalog_version: str,
    ) -> None:
        """Atomically replace validated server data while retaining user overrides."""
        new_map = dict(catalog_data)
        new_map.pop("sample_spec", None)
        if not new_map:
            raise ValueError("pricing catalog must contain at least one model")
        with self._lock:
            self._model_map = new_map
            self._pricing_version = catalog_version

    @property
    def pricing_version(self) -> str:
        """Hash of the currently loaded pricing data."""
        with self._lock:
            return self._pricing_version

    @property
    def model_count(self) -> int:
        """Number of models in the active pricing catalog."""
        with self._lock:
            return len(self._model_map)

    def model_mode(self, model: str) -> str | None:
        """Return the catalog operation mode for *model*, when declared.

        Provider instruments use this read-only resolver to distinguish chat,
        embedding, image, rerank, and other model surfaces without duplicating
        the server-distributed catalog or guessing from model-name substrings.
        """
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        with self._lock:
            resolved = self._resolve_model_entry(model)
            if resolved is None:
                return None
            raw_mode = resolved[1].get("mode")
        return raw_mode if isinstance(raw_mode, str) and raw_mode else None

    def close(self) -> None:
        """Stop background pricing refresh workers, if running."""
        if self._update_timer is not None:
            self._update_timer.cancel()
            self._update_timer = None
        self._server_refresh_stop.set()
        thread = self._server_refresh_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._server_refresh_thread = None

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model_entry(self, model: str) -> tuple[str, dict[str, Any]] | None:
        """Look up *model* and return the exact catalog key plus its entry."""
        # Exact match
        if model in self._model_map:
            return model, self._model_map[model]

        # Try common prefix patterns used by providers
        # e.g. "openai/gpt-4o" → "gpt-4o"
        if "/" in model:
            short = model.rsplit("/", 1)[-1]
            if short in self._model_map:
                return short, self._model_map[short]

        # Try matching without date suffix: "gpt-4o-2024-08-06" → "gpt-4o"
        # Walk from longest to shortest prefix split on "-"
        parts = model.split("-")
        for i in range(len(parts) - 1, 0, -1):
            candidate = "-".join(parts[:i])
            if candidate in self._model_map:
                return candidate, self._model_map[candidate]

        return None

    def _resolve_model(self, model: str) -> dict[str, Any] | None:
        """Look up *model* in the pricing data, trying alias resolution."""
        resolved = self._resolve_model_entry(model)
        return None if resolved is None else resolved[1]

    # ------------------------------------------------------------------
    # Background pricing update
    # ------------------------------------------------------------------

    def _schedule_update(self) -> None:
        """Schedule a non-blocking background pricing data refresh."""
        self._update_timer = threading.Timer(
            self._UPDATE_INTERVAL_SECONDS, self._background_update
        )
        self._update_timer.daemon = True
        self._update_timer.start()

    def _background_update(self) -> None:
        """Fetch fresh pricing data from LiteLLM's repository.

        Fail-silent: any exception is logged as a warning and swallowed.
        This method never blocks cost recording.
        """
        try:
            import urllib.request

            req = urllib.request.Request(
                self._UPDATE_URL,
                headers={"User-Agent": sdk_user_agent()},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")

            new_map: dict[str, dict[str, Any]] = json.loads(raw)
            new_map.pop("sample_spec", None)
            new_version = _compute_hash(raw)

            with self._lock:
                self._model_map = new_map
                self._pricing_version = new_version

            logger.info("Pricing data updated (version=%s, models=%d).", new_version, len(new_map))
        except Exception:
            logger.warning("Background pricing update failed; using cached data.", exc_info=True)

        # Re-schedule
        self._schedule_update()

    # ------------------------------------------------------------------
    # Server-based pricing refresh (US-044)
    # ------------------------------------------------------------------

    def refresh_from_server(self, endpoint: str) -> None:
        """Fetch fresh pricing data from the dexcost Control Layer.

        Fail-silent: any exception is logged as a warning and swallowed.
        The engine continues to use the bundled or previously loaded data.

        Args:
            endpoint: Base URL of the Control Layer (e.g.
                ``"https://api.dexcost.io"``).
        """
        import urllib.request

        url = f"{endpoint.rstrip('/')}/v1/api/pricing-data/latest"
        try:
            headers: dict[str, str] = {"User-Agent": sdk_user_agent()}
            with self._lock:
                api_key = self._api_key
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")

            payload = json.loads(raw)
            raw_data = payload.get("data")
            if not isinstance(raw_data, dict):
                logger.warning(
                    "Server pricing response had no usable data; keeping bundled pricing."
                )
                return
            server_data: dict[str, dict[str, Any]] = raw_data.get("data", {})
            if not isinstance(server_data, dict) or not server_data:
                logger.warning(
                    "Server pricing response had no usable data; keeping bundled pricing."
                )
                return

            server_data.pop("sample_spec", None)
            if not server_data:
                logger.warning(
                    "Server pricing response had no billable models; keeping bundled pricing."
                )
                return
            new_version = raw_data.get(
                "pricing_version", _compute_hash(json.dumps(server_data))
            )

            with self._lock:
                self._model_map = server_data
                self._pricing_version = new_version

            logger.info(
                "Pricing data refreshed from server (version=%s, models=%d).",
                new_version,
                len(server_data),
            )
        except Exception:
            logger.warning(
                "Failed to refresh pricing from server (%s); using cached data.",
                url,
                exc_info=True,
            )

    def start_background_refresh(
        self, endpoint: str, interval_seconds: float = _UPDATE_INTERVAL_SECONDS
    ) -> None:
        """Refresh from the control plane now and periodically in a daemon thread.

        Returns immediately.  The refresh runs in the background and is
        fail-silent.  Suitable for calling from ``dexcost.init()``.

        Args:
            endpoint: Base URL of the Control Layer.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self._server_refresh_thread is not None and self._server_refresh_thread.is_alive():
            return
        self._server_refresh_stop.clear()

        def _refresh_loop() -> None:
            while not self._server_refresh_stop.is_set():
                self.refresh_from_server(endpoint)
                self._server_refresh_stop.wait(interval_seconds)

        thread = threading.Thread(
            target=_refresh_loop,
            daemon=True,
            name="dexcost-pricing-refresh",
        )
        self._server_refresh_thread = thread
        thread.start()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _read_bundled_data() -> str:
    """Read the bundled ``model_cost_map.json`` shipped with the package."""
    ref = resources.files("dexcost").joinpath("data").joinpath("model_cost_map.json")
    return ref.read_text(encoding="utf-8")


def _compute_hash(raw: str) -> str:
    """Return a short SHA-256 prefix of *raw* for use as ``pricing_version``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
