"""GPU pricing engine — Phase 2 v2.

Dispatches on ``details.billing_model`` and applies the per-billing-model
math from spec §6. Four discriminator values (compute had 11; the GPU
fanout collapsed cleanly):

- ``per_gpu_second_active``     — Modal / RunPod / Replicate
- ``per_instance_hour``         — AWS EC2 GPU / GCP GCE bundled / Azure VM GPU
- ``per_gpu_hour_reserved``     — Lambda Labs / CoreWeave / GCP N1+accelerator
- ``per_vgpu_hour``             — Azure NVadsA10 v5 fractional (Decision #10)

Per Decision #7: **no per-runtime memory-unit conversion table**. VRAM tier
is encoded into the SKU key (``h100-80gb-sxm5`` vs ``a100-40gb`` are
separate catalog entries); no binary-vs-decimal divisor question.

Fail-silent contract (convention §9): every code path returns a usable
``GpuCost`` — the five-tier degradation ladder from convention §7 applies
(per-region SKU exact → per-runtime default → device-class fallback
[Decision #4] → universal _meta default → hardcoded constants → cost=0).

Decision #1 measurement-side fallback: the accountant sets
``details["_cgroup_scope_fallback"]`` when it degrades to self-PID-only /
no-container-scope / multi-container-pod-partial; the engine appends
that suffix to ``pricing_source`` and drops confidence to ``estimated``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

from dexcost.cloud_detect import CloudEnv

_log = logging.getLogger(__name__)

_warned_modes: set[str] = set()
_warn_lock = threading.Lock()


def _reset_warning_state() -> None:
    """Test-only: clear the warn-once tracking set."""
    with _warn_lock:
        _warned_modes.clear()


def _warn_once(mode: str, message: str) -> None:
    with _warn_lock:
        if mode in _warned_modes:
            return
        _warned_modes.add(mode)
    _log.warning(message)


# ─── Constants ───────────────────────────────────────────────────────────────

_HOUR_S = Decimal("3600")
_MS_PER_S = Decimal("1000")

# Tier-4 hardcoded constants — must mirror _meta defaults in gpu_prices.json.
_HARDCODED = {
    "per_instance_hour":     {"hourly_usd":     Decimal("55.04")},
    "per_gpu_second_active": {"gpu_second_usd": Decimal("0.000694")},
    "per_gpu_hour_reserved": {"gpu_hour_usd":   Decimal("3.99")},
    "per_vgpu_hour":         {"vgpu_hour_usd":  Decimal("0.454")},
}

# Decision #4 device-class default rates — cold-start fallback for unknown SKUs.
# Customer gets a rate within ~30% of true (estimated confidence) instead of $0
# when a brand-new NVIDIA SKU ships between catalog refreshes.
_DEVICE_CLASS_DEFAULTS = {
    "hopper":       {  # H100 / H200 generation
        "per_instance_hour":     Decimal("98.32"),
        "per_gpu_second_active": Decimal("0.001097"),
        "per_gpu_hour_reserved": Decimal("3.99"),
        "per_vgpu_hour":         Decimal("3.99"),
    },
    "ampere":       {  # A100 / A40 / A10 generation
        "per_instance_hour":     Decimal("32.77"),
        "per_gpu_second_active": Decimal("0.000833"),
        "per_gpu_hour_reserved": Decimal("2.20"),
        "per_vgpu_hour":         Decimal("2.20"),
    },
    "ada_lovelace": {  # L4 / L40S / RTX 4090 generation
        "per_instance_hour":     Decimal("12.00"),
        "per_gpu_second_active": Decimal("0.000400"),
        "per_gpu_hour_reserved": Decimal("1.50"),
        "per_vgpu_hour":         Decimal("1.50"),
    },
    "blackwell":    {  # B100 / B200 / GB200 generation (newer; estimated)
        "per_instance_hour":     Decimal("180.00"),
        "per_gpu_second_active": Decimal("0.002500"),
        "per_gpu_hour_reserved": Decimal("6.50"),
        "per_vgpu_hour":         Decimal("6.50"),
    },
}

# Substring patterns that map a productName to a device_class.
_DEVICE_CLASS_PATTERNS = (
    # Most specific first.
    ("blackwell",    ("b100", "b200", "gb200", "b300", "blackwell")),
    ("hopper",       ("h100", "h200", "hopper")),
    ("ada_lovelace", ("l4", "l40", "ada lovelace", "rtx 4090", "rtx 5090")),
    ("ampere",       ("a100", "a40", "a10", "ampere", "rtx 3090", "rtx a6000")),
)


def _detect_device_class(product_name_lower: str | None) -> str | None:
    """Match a normalized productName to a device class via substring."""
    if not product_name_lower:
        return None
    for cls, patterns in _DEVICE_CLASS_PATTERNS:
        for p in patterns:
            if p in product_name_lower:
                return cls
    return None


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GpuCost:
    """Resolved GPU cost for one ``gpu_cost`` event."""

    cost_usd: Decimal
    pricing_source: str
    cost_confidence: str  # computed | estimated | unknown


# ═════════════════════════════════════════════════════════════════════════════
# GpuPricingEngine
# ═════════════════════════════════════════════════════════════════════════════


class GpuPricingEngine:
    """Resolve GPU cost per ``gpu_cost`` event details.

    Args:
        catalog_path: optional override; ``None`` uses the bundled
            ``data/gpu_prices.json`` catalog.
    """

    def __init__(self, catalog_path: str | Path | None = None) -> None:
        self._catalog: dict[str, Any] = {}
        self._catalog_path = catalog_path
        self._catalog_version: str = "unknown"
        self._load()

    # ------------------------------------------------------------------
    # Catalog loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._catalog_path is not None:
                raw = Path(self._catalog_path).read_text(encoding="utf-8")
            else:
                raw = (
                    resources.files("dexcost")
                    .joinpath("data")
                    .joinpath("gpu_prices.json")
                    .read_text(encoding="utf-8")
                )
        except FileNotFoundError:
            _warn_once(
                "gpu_catalog_missing",
                "gpu catalog file not found; falling back to hardcoded "
                "per-billing-model defaults",
            )
            return
        except OSError as exc:
            _warn_once(
                "gpu_catalog_unreadable",
                f"gpu catalog unreadable ({exc}); falling back to hardcoded",
            )
            return

        try:
            self._catalog = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn_once(
                "gpu_catalog_malformed",
                f"gpu catalog malformed JSON ({exc}); falling back to hardcoded",
            )
            self._catalog = {}
            return

        meta = self._catalog.get("_meta", {})
        self._catalog_version = str(meta.get("version", "unknown"))

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    # ------------------------------------------------------------------
    # Public entry point — Tier-5 wrapper + Decision #1 suffix application
    # ------------------------------------------------------------------

    def resolve_gpu_cost(
        self,
        details: dict[str, Any],
        cloud_env: CloudEnv,
        window_s: Decimal | None = None,
    ) -> GpuCost:
        """Compute cost for one ``gpu_cost`` event.

        Tier 5 wraps the dispatch in try/except so a pricing bug cannot
        break task finalize. Decision #1 ``_cgroup_scope_fallback`` is
        applied AFTER the rate is resolved — it suffixes pricing_source
        and drops confidence to ``estimated``.
        """
        billing_model = (details or {}).get("billing_model") or "unknown"
        try:
            cost = self._dispatch(billing_model, details, cloud_env, window_s)
        except Exception as exc:  # noqa: BLE001 — Tier 5 fail-silent
            _warn_once(
                f"gpu_pricing_failure:{billing_model}",
                f"gpu pricing failed for billing_model={billing_model}: "
                f"{exc}; emitting cost_usd=0",
            )
            return GpuCost(
                cost_usd=Decimal("0"),
                pricing_source=f"gpu_catalog:error:{billing_model}",
                cost_confidence="unknown",
            )

        # Decision #1 measurement-side fallback suffix.
        scope_fb = (details or {}).get("_cgroup_scope_fallback")
        if scope_fb:
            return GpuCost(
                cost_usd=cost.cost_usd,
                pricing_source=cost.pricing_source + f":{scope_fb}",
                cost_confidence="estimated",
            )
        return cost

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        billing_model: str,
        details: dict[str, Any],
        cloud_env: CloudEnv,
        window_s: Decimal | None,
    ) -> GpuCost:
        if billing_model == "per_gpu_second_active":
            return self._per_gpu_second(details, cloud_env)
        if billing_model == "per_instance_hour":
            return self._per_instance_hour(details, cloud_env, window_s)
        if billing_model == "per_gpu_hour_reserved":
            return self._per_gpu_hour(details, cloud_env, window_s)
        if billing_model == "per_vgpu_hour":
            return self._per_vgpu_hour(details, cloud_env, window_s)
        _warn_once(
            f"gpu_unsupported_billing_model:{billing_model}",
            f"gpu pricing has no math for billing_model={billing_model}",
        )
        return GpuCost(
            cost_usd=Decimal("0"),
            pricing_source=f"gpu_catalog:unsupported:{billing_model}",
            cost_confidence="unknown",
        )

    # ─── per_gpu_second_active ────────────────────────────────────────────

    def _per_gpu_second(self, details, cloud_env) -> GpuCost:
        """gpu_seconds_used × rate_per_gpu_second_usd. Highest-precision regime."""
        provider = cloud_env.provider
        gpu_sku = details.get("gpu_sku")
        rate, source, confidence = self._resolve_per_gpu_second_rate(
            provider, gpu_sku, details,
        )
        gpu_seconds = Decimal(str(details["gpu_seconds_used"]))
        cost = gpu_seconds * rate
        return GpuCost(cost, source, confidence)

    def _resolve_per_gpu_second_rate(self, provider, gpu_sku, details):
        """Walk per_gpu_second_active providers; handle RunPod's on_demand nesting."""
        if provider and gpu_sku:
            block = self._catalog.get(provider, {}).get("per_gpu_second_active")
            if isinstance(block, dict):
                default = block.get("default", {})
                # Direct lookup (Modal, Replicate shape)
                for key, entry in default.items():
                    if isinstance(entry, dict) and entry.get("gpu_sku") == gpu_sku:
                        try:
                            return (
                                Decimal(str(entry["gpu_second_usd"])),
                                f"gpu_catalog:{provider}:per_gpu_second_active:{key}",
                                "computed",
                            )
                        except (KeyError, InvalidOperation):
                            pass
                    # Nested lookup (RunPod on_demand/community_cloud)
                    if isinstance(entry, dict):
                        for sku_key, sku_entry in entry.items():
                            if (isinstance(sku_entry, dict)
                                    and sku_entry.get("gpu_sku") == gpu_sku):
                                try:
                                    return (
                                        Decimal(str(sku_entry["gpu_second_usd"])),
                                        f"gpu_catalog:{provider}:per_gpu_second_active:{key}:{sku_key}",
                                        "computed",
                                    )
                                except (KeyError, InvalidOperation):
                                    pass
        # Decision #4 device-class fallback.
        return self._device_class_or_meta_fallback(
            details, "per_gpu_second_active", "gpu_second_usd",
        )

    # ─── per_instance_hour ────────────────────────────────────────────────

    def _per_instance_hour(self, details, cloud_env, window_s) -> GpuCost:
        """share_factor × window_hours × instance_hourly_usd."""
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        provider = cloud_env.provider
        region = details.get("region")
        instance_type = details.get("instance_type") or cloud_env.instance_type
        hourly_rate, source, confidence = self._resolve_per_instance_rate(
            provider, region, instance_type, details,
        )
        gpu_count = Decimal(str(details["gpu_count"]))
        gpu_seconds = Decimal(str(details["gpu_seconds_used"]))
        if gpu_count <= 0 or window_s <= 0:
            return GpuCost(Decimal("0"), source, confidence)
        share_factor = gpu_seconds / (gpu_count * window_s)
        task_instance_hours = share_factor * (window_s / _HOUR_S)
        cost = task_instance_hours * hourly_rate
        return GpuCost(cost, source, confidence)

    def _resolve_per_instance_rate(self, provider, region, instance_type, details):
        # Provider-block keys: aws.ec2_gpu, gcp.gce_gpu_bundled, azure.vm_gpu
        block_keys = {
            "aws":   "ec2_gpu",
            "gcp":   "gce_gpu_bundled",
            "azure": "vm_gpu",
        }
        block_key = block_keys.get(provider)
        if provider and block_key and instance_type and region:
            block = self._catalog.get(provider, {}).get(block_key, {})
            regions = block.get("regions", {})
            entry = regions.get(region, {}).get("instance_types", {}).get(instance_type)
            if entry:
                try:
                    return (
                        Decimal(str(entry["hourly_usd"])),
                        f"gpu_catalog:{provider}:{block_key}:{region}:{instance_type}",
                        "computed",
                    )
                except (KeyError, InvalidOperation):
                    pass
        return self._device_class_or_meta_fallback(
            details, "per_instance_hour", "hourly_usd",
        )

    # ─── per_gpu_hour_reserved ────────────────────────────────────────────

    def _per_gpu_hour(self, details, cloud_env, window_s) -> GpuCost:
        """share_factor × window_hours × gpu_count × gpu_hourly_usd."""
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        provider = cloud_env.provider
        gpu_sku = details.get("gpu_sku")
        gpu_hour_usd, source, confidence = self._resolve_per_gpu_hour_rate(
            provider, gpu_sku, details,
        )
        gpu_count = Decimal(str(details["gpu_count"]))
        gpu_seconds = Decimal(str(details["gpu_seconds_used"]))
        if gpu_count <= 0 or window_s <= 0:
            return GpuCost(Decimal("0"), source, confidence)
        share_factor = gpu_seconds / (gpu_count * window_s)
        task_gpu_hours = share_factor * (window_s / _HOUR_S) * gpu_count
        cost = task_gpu_hours * gpu_hour_usd
        return GpuCost(cost, source, confidence)

    def _resolve_per_gpu_hour_rate(self, provider, gpu_sku, details):
        if provider and gpu_sku:
            block = self._catalog.get(provider, {}).get("per_gpu_hour_reserved")
            if isinstance(block, dict):
                default = block.get("default", {})
                for key, entry in default.items():
                    if isinstance(entry, dict) and entry.get("gpu_sku") == gpu_sku:
                        try:
                            return (
                                Decimal(str(entry["gpu_hour_usd"])),
                                f"gpu_catalog:{provider}:per_gpu_hour_reserved:{key}",
                                "computed",
                            )
                        except (KeyError, InvalidOperation):
                            pass
        # GCP N1+accelerator path (Decision #9) — separate block.
        if provider == "gcp" and gpu_sku:
            block = self._catalog["gcp"].get("gce_gpu_attached", {})
            region = details.get("region")
            if region:
                accelerators = block.get("regions", {}).get(region, {}).get("accelerator_types", {})
                for acc_key, entry in accelerators.items():
                    if entry.get("gpu_sku") == gpu_sku:
                        try:
                            return (
                                Decimal(str(entry["gpu_hour_usd"])),
                                f"gpu_catalog:gcp:gce_gpu_attached:{region}:{acc_key}",
                                "computed",
                            )
                        except (KeyError, InvalidOperation):
                            pass
        return self._device_class_or_meta_fallback(
            details, "per_gpu_hour_reserved", "gpu_hour_usd",
        )

    # ─── per_vgpu_hour (Azure NVadsA10 v5 fractional — Decision #10) ─────

    def _per_vgpu_hour(self, details, cloud_env, window_s) -> GpuCost:
        """share_factor × window_hours × vgpu_hourly_usd. vCount=1; frac in rate."""
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        provider = cloud_env.provider
        region = details.get("region")
        instance_type = details.get("instance_type") or cloud_env.instance_type
        vgpu_hour_usd, source, confidence = self._resolve_per_vgpu_rate(
            provider, region, instance_type, details,
        )
        gpu_seconds = Decimal(str(details["gpu_seconds_used"]))
        if window_s <= 0:
            return GpuCost(Decimal("0"), source, confidence)
        share_factor = gpu_seconds / window_s
        task_vgpu_hours = share_factor * (window_s / _HOUR_S)
        cost = task_vgpu_hours * vgpu_hour_usd
        return GpuCost(cost, source, confidence)

    def _resolve_per_vgpu_rate(self, provider, region, instance_type, details):
        if provider == "azure" and instance_type and region:
            block = self._catalog["azure"].get("vm_vgpu", {})
            entry = block.get("regions", {}).get(region, {}).get("instance_types", {}).get(instance_type)
            if entry:
                try:
                    return (
                        Decimal(str(entry["vgpu_hour_usd"])),
                        f"gpu_catalog:azure:vm_vgpu:{region}:{instance_type}",
                        "computed",
                    )
                except (KeyError, InvalidOperation):
                    pass
        return self._device_class_or_meta_fallback(
            details, "per_vgpu_hour", "vgpu_hour_usd",
        )

    # ─── Tier-3 (device-class fallback) → Tier-4 (meta default) → Tier-4 hardcoded ─

    def _device_class_or_meta_fallback(
        self, details: dict[str, Any], billing_model: str, rate_key: str,
    ) -> tuple[Decimal, str, str]:
        """Tier-3 (Decision #4 device-class) → Tier-3 (meta) → Tier-4 (hardcoded).

        Returns (rate, pricing_source, cost_confidence). Always succeeds.
        """
        # Tier-3a: device-class fallback via productName substring matching.
        product_name = details.get("_nvml_product_name_lower")
        device_class = _detect_device_class(product_name)
        if device_class and billing_model in (
            "per_instance_hour", "per_gpu_second_active",
            "per_gpu_hour_reserved", "per_vgpu_hour",
        ):
            rate = _DEVICE_CLASS_DEFAULTS[device_class][billing_model]
            _warn_once(
                f"gpu_sku_unknown:{product_name}",
                f"GPU SKU not in catalog (productName={product_name!r}); "
                f"falling back to device_class={device_class} default rate "
                f"(~30% accuracy band)",
            )
            return (
                rate,
                f"gpu_catalog:device_class_fallback:{device_class}:{billing_model}",
                "estimated",
            )

        # Tier-3b: universal _meta default.
        meta = self._catalog.get("_meta", {})
        meta_key = f"default_{billing_model}_usd"
        if meta_key in meta:
            try:
                return (
                    Decimal(str(meta[meta_key])),
                    f"gpu_catalog:default:{billing_model}",
                    "estimated",
                )
            except (InvalidOperation, TypeError):
                pass

        # Tier-4: hardcoded constants.
        hc = _HARDCODED[billing_model]
        return (
            hc[rate_key],
            f"gpu_catalog:hardcoded:{billing_model}",
            "estimated",
        )
