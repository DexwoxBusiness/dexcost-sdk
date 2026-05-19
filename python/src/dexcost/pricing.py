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
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._custom_pricing: dict[str, CustomPricing] = {}
        self._lock = threading.Lock()
        self._api_key: str | None = api_key

        # Load bundled pricing data
        if data_path is not None:
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
        self._pricing_version = _compute_hash(raw)

        # Background updater
        self._update_timer: threading.Timer | None = None
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
    ) -> CostResult:
        """Calculate the cost for an LLM call.

        Args:
            model: Model identifier (e.g. ``"gpt-4o"``).
            input_tokens: Number of input (prompt) tokens.
            output_tokens: Number of output (completion) tokens.
            cached_tokens: Number of cached input tokens that receive a
                discount.  These are *subtracted* from ``input_tokens`` for
                pricing at the full input rate, and charged at the cached rate
                instead.
            cache_creation_tokens: Number of input tokens written to cache
                (Anthropic-specific).  Charged at the higher
                ``cache_creation_input_token_cost`` rate instead of the normal
                input rate.

        Returns:
            A :class:`CostResult` with ``cost_usd``, ``cost_confidence``,
            ``pricing_source``, and ``pricing_version``.
        """
        # 1. Check custom pricing first
        with self._lock:
            custom = self._custom_pricing.get(model)
        if custom is not None:
            cost = custom.input_per_1k * Decimal(str(input_tokens)) / Decimal(
                "1000"
            ) + custom.output_per_1k * Decimal(str(output_tokens)) / Decimal("1000")
            return CostResult(
                cost_usd=cost,
                cost_confidence="computed",
                pricing_source="custom",
                pricing_version=self._pricing_version,
            )

        # 2. Resolve from bundled LiteLLM data
        model_info = self._resolve_model(model)
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
                pricing_version=self._pricing_version,
            )

        # JSON stores prices as float literals (e.g. 0.0000025).  Python's
        # json.loads produces exact IEEE 754 doubles for these simple values
        # and str() renders them without precision loss, so Decimal(str(float))
        # is safe here.  Avoid arithmetic on the raw floats before conversion.
        input_cost_per_token = Decimal(str(model_info.get("input_cost_per_token", 0)))
        output_cost_per_token = Decimal(str(model_info.get("output_cost_per_token", 0)))
        cache_read_cost_per_token = Decimal(str(model_info.get("cache_read_input_token_cost", 0)))
        cache_creation_cost_per_token = Decimal(
            str(model_info.get("cache_creation_input_token_cost", 0))
        )

        # Cached tokens (read + creation) are subtracted from input_tokens
        # and charged at their respective rates.
        effective_cached = min(cached_tokens, input_tokens)
        remaining = input_tokens - effective_cached
        effective_creation = min(cache_creation_tokens, remaining)
        non_cached_input = remaining - effective_creation

        cost = (
            input_cost_per_token * Decimal(str(non_cached_input))
            + cache_read_cost_per_token * Decimal(str(effective_cached))
            + cache_creation_cost_per_token * Decimal(str(effective_creation))
            + output_cost_per_token * Decimal(str(output_tokens))
        )

        return CostResult(
            cost_usd=cost,
            cost_confidence="computed",
            pricing_source="litellm",
            pricing_version=self._pricing_version,
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

    @property
    def pricing_version(self) -> str:
        """Hash of the currently loaded pricing data."""
        return self._pricing_version

    def close(self) -> None:
        """Cancel the background update timer, if running."""
        if self._update_timer is not None:
            self._update_timer.cancel()
            self._update_timer = None

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str) -> dict[str, Any] | None:
        """Look up *model* in the pricing data, trying alias resolution."""
        # Exact match
        if model in self._model_map:
            return self._model_map[model]

        # Try common prefix patterns used by providers
        # e.g. "openai/gpt-4o" → "gpt-4o"
        if "/" in model:
            short = model.rsplit("/", 1)[-1]
            if short in self._model_map:
                return self._model_map[short]

        # Try matching without date suffix: "gpt-4o-2024-08-06" → "gpt-4o"
        # Walk from longest to shortest prefix split on "-"
        parts = model.split("-")
        for i in range(len(parts) - 1, 0, -1):
            candidate = "-".join(parts[:i])
            if candidate in self._model_map:
                return self._model_map[candidate]

        return None

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

            req = urllib.request.Request(self._UPDATE_URL, headers={"User-Agent": "dexcost"})
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
            headers: dict[str, str] = {"User-Agent": "dexcost-sdk"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
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

    def start_background_refresh(self, endpoint: str) -> None:
        """Launch a non-blocking daemon thread to refresh pricing from the server.

        Returns immediately.  The refresh runs in the background and is
        fail-silent.  Suitable for calling from ``dexcost.init()``.

        Args:
            endpoint: Base URL of the Control Layer.
        """
        thread = threading.Thread(
            target=self.refresh_from_server,
            args=(endpoint,),
            daemon=True,
            name="dexcost-pricing-refresh",
        )
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
