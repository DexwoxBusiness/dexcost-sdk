"""Compute pricing engine — dispatches on ``details.billing_model`` and
applies the per-billing-model math from spec §6.

The per-runtime memory-unit conversion table (Decision #7) is pinned at the
catalog-lookup boundary in §6.2 of the spec; the implementation enforces it
via two Decimal divisor constants (decimal GB vs binary GiB) selected per
billing model. Confusing them silently over-attributes Fargate memory cost
by ~4.86% — the pricing tests pin the divisor choice per model.

Fail-silent contract (convention §9): every code path returns a usable
``ComputeCost`` — the five-tier degradation ladder from convention §7
applies (per-region exact → per-runtime default → universal _meta default →
hardcoded constants → cost=0 with warning).
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

# Module-level set of warning-mode tokens already logged in this process.
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


# ─── Conversion constants (Decision #7 pinned table) ─────────────────────────

_GB_DECIMAL = Decimal("1000000000")           # 10^9 bytes — Lambda / Azure Funcs / Vercel
_GIB_BINARY = Decimal(1024 * 1024 * 1024)     # 2^30 bytes — Fargate / Cloud Run
_HOUR_S = Decimal("3600")
_MS_PER_S = Decimal("1000")

# ─── Tier-4 hardcoded constants (must mirror _meta defaults) ─────────────────

_HARDCODED = {
    "lambda":             {"request_usd": Decimal("0.0000002"),
                           "gb_second_usd": Decimal("0.0000166667")},
    "fargate":            {"vcpu_second_usd": Decimal("0.0000111111"),
                           "gib_second_usd": Decimal("0.0000012222")},
    "cloud_run_request":  {"request_usd": Decimal("0.0000004"),
                           "vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "cloud_run_instance": {"vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "cloud_functions":    {"request_usd": Decimal("0.0000004"),
                           "vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "azure_functions":    {"execution_usd": Decimal("0.0000002"),
                           "gb_second_usd": Decimal("0.000016")},
    "vercel_fluid":       {"active_cpu_hour_usd": Decimal("0.128"),
                           "memory_gb_hour_usd": Decimal("0.0106"),
                           "invocation_usd": Decimal("0.000000600")},
    "ec2":                {"vcpu_hour_usd": Decimal("0.0464")},
    "gce":                {"vcpu_hour_usd": Decimal("0.0475")},
    "azure_vm":           {"vcpu_hour_usd": Decimal("0.046")},
    "k8s_pod":            {"vcpu_hour_usd": Decimal("0.0464")},
}


@dataclass(frozen=True)
class ComputeCost:
    cost_usd: Decimal
    pricing_source: str
    cost_confidence: str  # exact | computed | estimated | unknown


# ═════════════════════════════════════════════════════════════════════════════
# ComputePricingEngine
# ═════════════════════════════════════════════════════════════════════════════


class ComputePricingEngine:
    """Resolve compute cost per ``compute_cost`` event details.

    Args:
        catalog_path: optional override path; ``None`` uses bundled
            ``data/compute_prices.json``.
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
                    .joinpath("compute_prices.json")
                    .read_text(encoding="utf-8")
                )
        except FileNotFoundError:
            _warn_once(
                "catalog_missing",
                "compute catalog file not found; falling back to hardcoded "
                "per-billing-model defaults",
            )
            return
        except OSError as exc:
            _warn_once(
                "catalog_unreadable",
                f"compute catalog unreadable ({exc}); falling back to "
                "hardcoded per-billing-model defaults",
            )
            return

        try:
            self._catalog = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn_once(
                "catalog_malformed",
                f"compute catalog malformed JSON ({exc}); falling back to "
                "hardcoded per-billing-model defaults",
            )
            self._catalog = {}
            return

        meta = self._catalog.get("_meta", {})
        self._catalog_version = str(meta.get("version", "unknown"))

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    # ------------------------------------------------------------------
    # Public entry point — Tier 5 wrapper
    # ------------------------------------------------------------------

    def resolve_compute_cost(
        self,
        details: dict[str, Any],
        cloud_env: CloudEnv,
        overrides: dict[str, str] | None,
        window_s: Decimal | None = None,
    ) -> ComputeCost:
        """Compute cost for one ``compute_cost`` event.

        Returns a usable ``ComputeCost`` in every case — Tier 5 wraps the
        dispatch in a try/except so a pricing bug cannot break task finalize.
        """
        billing_model = (details or {}).get("billing_model") or "unknown"
        overrides = overrides or {}
        try:
            return self._dispatch(billing_model, details, cloud_env,
                                  overrides, window_s)
        except Exception as exc:  # noqa: BLE001 — Tier 5 fail-silent
            _warn_once(
                f"compute_failure:{billing_model}",
                f"compute pricing failed for billing_model={billing_model}: "
                f"{exc}; emitting cost_usd=0",
            )
            return ComputeCost(
                cost_usd=Decimal("0"),
                pricing_source=f"compute_catalog:error:{billing_model}",
                cost_confidence="unknown",
            )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        billing_model: str,
        details: dict[str, Any],
        cloud_env: CloudEnv,
        overrides: dict[str, str],
        window_s: Decimal | None,
    ) -> ComputeCost:
        # Cloud Run override — flip the math BEFORE catalog lookup.
        if billing_model == "cloud_run_request" and \
                overrides.get("cloud_run") == "instance":
            return self._cloud_run_instance_override(details, window_s)

        if billing_model == "lambda":
            return self._lambda(details)
        if billing_model == "fargate":
            return self._fargate(details, window_s)
        if billing_model == "cloud_run_request":
            return self._cloud_run_request(details)
        if billing_model == "cloud_run_instance":
            return self._cloud_run_instance_override(details, window_s)
        if billing_model == "cloud_functions":
            return self._cloud_functions(details)
        if billing_model == "azure_functions":
            return self._azure_functions(details)
        if billing_model == "vercel_fluid":
            return self._vercel(details)
        if billing_model in ("ec2", "gce", "azure_vm"):
            return self._iaas_share(billing_model, details, cloud_env, window_s)
        if billing_model == "k8s_pod":
            return self._k8s_pod_limits(details, window_s)

        # Unknown / unrecognized billing model — cost=0, log once, ship.
        _warn_once(
            f"unsupported_billing_model:{billing_model}",
            f"compute pricing has no math for billing_model={billing_model}; "
            "emitting cost_usd=0",
        )
        return ComputeCost(
            cost_usd=Decimal("0"),
            pricing_source=f"compute_catalog:unsupported:{billing_model}",
            cost_confidence="unknown",
        )

    # ─── Lambda ──────────────────────────────────────────────────────────

    def _lambda(self, details: dict[str, Any]) -> ComputeCost:
        region = details.get("region")
        architecture = details.get("architecture") or "x86_64"
        rate, source, confidence = self._resolve_lambda_rate(region, architecture)
        duration_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        memory_gb = Decimal(str(details["memory_bytes_limit"])) / _GB_DECIMAL
        gb_seconds = memory_gb * duration_s
        invocations = Decimal(str(details["invocation_count"]))
        cost = (
            invocations * rate["request_usd"]
            + gb_seconds * rate["gb_second_usd"]
        )
        return ComputeCost(cost, source, confidence)

    def _resolve_lambda_rate(self, region, architecture):
        block = self._catalog.get("aws", {}).get("lambda")
        if isinstance(block, dict):
            regions = block.get("regions", {})
            if region and region in regions:
                arch_block = regions[region].get(architecture)
                if arch_block:
                    return (
                        self._parse_rate_block(arch_block,
                                               ("request_usd", "gb_second_usd")),
                        f"compute_catalog:aws:lambda:{region}:{architecture}",
                        "computed",
                    )
            default = block.get("default", {}).get(architecture)
            if default:
                return (
                    self._parse_rate_block(default,
                                           ("request_usd", "gb_second_usd")),
                    f"compute_catalog:aws:lambda:default:{architecture}",
                    "estimated",
                )
        # Tier 3 → _meta defaults.
        meta = self._catalog.get("_meta", {})
        try:
            return (
                {
                    "request_usd": Decimal(str(meta["default_lambda_request_usd"])),
                    "gb_second_usd": Decimal(str(meta["default_lambda_gb_second_usd"])),
                },
                "compute_catalog:default:lambda",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["lambda"],
                    "compute_catalog:hardcoded:lambda", "estimated")

    # ─── Fargate ─────────────────────────────────────────────────────────

    def _fargate(self, details, window_s):
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        region = details.get("region")
        architecture = details.get("architecture") or "x86_64"
        rate, source, confidence = self._resolve_fargate_rate(region, architecture)
        memory_gib = Decimal(str(details["memory_bytes_limit"])) / _GIB_BINARY
        vcpu_count = Decimal(str(details["vcpu_count"]))
        cost = (
            (vcpu_count * window_s) * rate["vcpu_second_usd"]
            + (memory_gib * window_s) * rate["gib_second_usd"]
        )
        return ComputeCost(cost, source, confidence)

    def _resolve_fargate_rate(self, region, architecture):
        block = self._catalog.get("aws", {}).get("fargate")
        if isinstance(block, dict):
            regions = block.get("regions", {})
            if region and region in regions:
                arch_block = regions[region].get(architecture)
                if arch_block:
                    return (
                        self._parse_rate_block(arch_block,
                                               ("vcpu_second_usd", "gib_second_usd")),
                        f"compute_catalog:aws:fargate:{region}:{architecture}",
                        "computed",
                    )
            default = block.get("default", {}).get(architecture)
            if default:
                return (
                    self._parse_rate_block(default,
                                           ("vcpu_second_usd", "gib_second_usd")),
                    f"compute_catalog:aws:fargate:default:{architecture}",
                    "estimated",
                )
        meta = self._catalog.get("_meta", {})
        try:
            return (
                {
                    "vcpu_second_usd": Decimal(str(meta["default_fargate_vcpu_second_usd"])),
                    "gib_second_usd": Decimal(str(meta["default_fargate_gib_second_usd"])),
                },
                "compute_catalog:default:fargate",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["fargate"],
                    "compute_catalog:hardcoded:fargate", "estimated")

    # ─── Cloud Run (request-based, default) ──────────────────────────────

    def _cloud_run_request(self, details):
        # Decision #1: Cloud Run defaults to request-based with estimated
        # confidence — the container cannot discover the actual billing mode.
        region = details.get("region")
        rate, source, confidence = self._resolve_cloud_run_rate(region)
        # Override the source string to make the default-mode origin obvious.
        if confidence == "computed":
            source = "compute_catalog:cloud_run:request_based_default"
            confidence = "estimated"
        else:
            source = "compute_catalog:cloud_run:request_based_default"
        duration_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        memory_gib = Decimal(str(details["memory_bytes_limit"])) / _GIB_BINARY
        vcpu_count = Decimal(str(details["vcpu_count"]))
        invocations = Decimal(str(details["invocation_count"]))
        cost = (
            invocations * rate["request_usd"]
            + (vcpu_count * duration_s) * rate["vcpu_second_usd"]
            + (memory_gib * duration_s) * rate["gib_second_usd"]
        )
        return ComputeCost(cost, source, confidence)

    def _cloud_run_instance_override(self, details, window_s):
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        region = details.get("region")
        rate, _source, _confidence = self._resolve_cloud_run_rate(region)
        memory_gib = Decimal(str(details["memory_bytes_limit"])) / _GIB_BINARY
        vcpu_count = Decimal(str(details["vcpu_count"]))
        cost = (
            (vcpu_count * window_s) * rate["vcpu_second_usd"]
            + (memory_gib * window_s) * rate["gib_second_usd"]
        )
        return ComputeCost(
            cost,
            "compute_catalog:cloud_run:instance_override",
            "computed",
        )

    def _resolve_cloud_run_rate(self, region):
        block = self._catalog.get("gcp", {}).get("cloud_run")
        if isinstance(block, dict):
            regions = block.get("regions", {})
            if region and region in regions:
                return (
                    self._parse_rate_block(
                        regions[region],
                        ("request_usd", "vcpu_second_usd", "gib_second_usd"),
                    ),
                    f"compute_catalog:gcp:cloud_run:{region}",
                    "computed",
                )
            default = block.get("default")
            if default:
                return (
                    self._parse_rate_block(
                        default,
                        ("request_usd", "vcpu_second_usd", "gib_second_usd"),
                    ),
                    "compute_catalog:gcp:cloud_run:default",
                    "estimated",
                )
        meta = self._catalog.get("_meta", {})
        try:
            return (
                {
                    "request_usd": Decimal(str(meta["default_cloud_run_request_usd"])),
                    "vcpu_second_usd": Decimal(str(meta["default_cloud_run_vcpu_second_usd"])),
                    "gib_second_usd": Decimal(str(meta["default_cloud_run_gib_second_usd"])),
                },
                "compute_catalog:default:cloud_run",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["cloud_run_request"],
                    "compute_catalog:hardcoded:cloud_run", "estimated")

    # ─── Cloud Functions Gen2 (Cloud Run pricing under the hood) ─────────

    def _cloud_functions(self, details):
        region = details.get("region")
        rate, source, confidence = self._resolve_cloud_run_rate(region)
        # Surface "cloud_functions" in the pricing_source for dashboard
        # break-out even though the math/rate is shared with Cloud Run.
        source = source.replace("cloud_run", "cloud_functions")
        duration_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        memory_gib = Decimal(str(details["memory_bytes_limit"])) / _GIB_BINARY
        vcpu_count = Decimal(str(details["vcpu_count"]))
        invocations = Decimal(str(details["invocation_count"]))
        cost = (
            invocations * rate["request_usd"]
            + (vcpu_count * duration_s) * rate["vcpu_second_usd"]
            + (memory_gib * duration_s) * rate["gib_second_usd"]
        )
        return ComputeCost(cost, source, confidence)

    # ─── Azure Functions Consumption ─────────────────────────────────────

    def _azure_functions(self, details):
        region = details.get("region")
        rate, source, confidence = self._resolve_azure_functions_rate(region)
        duration_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        memory_gb = Decimal(str(details["memory_bytes_limit"])) / _GB_DECIMAL
        invocations = Decimal(str(details["invocation_count"]))
        cost = (
            invocations * rate["execution_usd"]
            + (memory_gb * duration_s) * rate["gb_second_usd"]
        )
        return ComputeCost(cost, source, confidence)

    def _resolve_azure_functions_rate(self, region):
        block = self._catalog.get("azure", {}).get("functions_consumption")
        if isinstance(block, dict):
            regions = block.get("regions", {})
            if region and region in regions:
                return (
                    self._parse_rate_block(
                        regions[region],
                        ("execution_usd", "gb_second_usd"),
                    ),
                    f"compute_catalog:azure:functions_consumption:{region}",
                    "computed",
                )
            default = block.get("default")
            if default:
                return (
                    self._parse_rate_block(
                        default, ("execution_usd", "gb_second_usd"),
                    ),
                    "compute_catalog:azure:functions_consumption:default",
                    "estimated",
                )
        meta = self._catalog.get("_meta", {})
        try:
            return (
                {
                    "execution_usd": Decimal(str(meta["default_azure_functions_execution_usd"])),
                    "gb_second_usd": Decimal(str(meta["default_azure_functions_gb_second_usd"])),
                },
                "compute_catalog:default:azure_functions",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["azure_functions"],
                    "compute_catalog:hardcoded:azure_functions", "estimated")

    # ─── Vercel Fluid ────────────────────────────────────────────────────

    def _vercel(self, details):
        rate, source, confidence = self._resolve_vercel_rate()
        duration_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        memory_gb = Decimal(str(details["memory_bytes_limit"])) / _GB_DECIMAL
        invocations = Decimal(str(details["invocation_count"]))
        active_cpu_hours = duration_s / _HOUR_S
        memory_gb_hours = memory_gb * (duration_s / _HOUR_S)
        cost = (
            invocations * rate["invocation_usd"]
            + active_cpu_hours * rate["active_cpu_hour_usd"]
            + memory_gb_hours * rate["memory_gb_hour_usd"]
        )
        return ComputeCost(cost, source, confidence)

    def _resolve_vercel_rate(self):
        block = self._catalog.get("vercel", {}).get("fluid")
        if isinstance(block, dict):
            default = block.get("default")
            if default:
                return (
                    self._parse_rate_block(
                        default,
                        ("active_cpu_hour_usd", "memory_gb_hour_usd",
                         "invocation_usd"),
                    ),
                    "compute_catalog:vercel:fluid",
                    "computed",
                )
        meta = self._catalog.get("_meta", {})
        try:
            return (
                {
                    "active_cpu_hour_usd": Decimal(str(meta["default_vercel_cpu_hour_usd"])),
                    "memory_gb_hour_usd": Decimal(str(meta["default_vercel_memory_gb_hour_usd"])),
                    "invocation_usd": Decimal("0.000000600"),
                },
                "compute_catalog:default:vercel",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["vercel_fluid"],
                    "compute_catalog:hardcoded:vercel", "estimated")

    # ─── EC2 / GCE / Azure VM share ──────────────────────────────────────

    def _iaas_share(self, billing_model, details, cloud_env, window_s):
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        instance_type = cloud_env.instance_type
        region = details.get("region")
        instance_hourly, source, confidence = self._resolve_iaas_rate(
            billing_model, region, instance_type,
        )
        vcpu_count = Decimal(str(details["vcpu_count"]))
        vcpu_seconds = Decimal(str(details["vcpu_seconds_used"]))
        if vcpu_count <= 0 or window_s <= 0:
            return ComputeCost(Decimal("0"), source, confidence)
        share_factor = vcpu_seconds / (vcpu_count * window_s)
        task_instance_hours = share_factor * (window_s / _HOUR_S)
        cost = task_instance_hours * instance_hourly
        return ComputeCost(cost, source, confidence)

    def _resolve_iaas_rate(self, billing_model, region, instance_type):
        provider_key, runtime_key = {
            "ec2": ("aws", "ec2"),
            "gce": ("gcp", "gce"),
            "azure_vm": ("azure", "vm"),
        }[billing_model]
        block = self._catalog.get(provider_key, {}).get(runtime_key)
        if isinstance(block, dict):
            regions = block.get("regions", {})
            if region and region in regions and instance_type:
                instances = regions[region].get("instance_types", {})
                sku = instances.get(instance_type)
                if sku:
                    try:
                        return (
                            Decimal(str(sku["hourly_usd"])),
                            f"compute_catalog:{provider_key}:{runtime_key}:"
                            f"{region}:{instance_type}",
                            "computed",
                        )
                    except (KeyError, InvalidOperation):
                        pass
            try:
                default_hourly = Decimal(str(block["default_vcpu_hour_usd"]))
                return (
                    default_hourly,  # per-vCPU-hour fallback
                    f"compute_catalog:{provider_key}:{runtime_key}:default",
                    "estimated",
                )
            except (KeyError, InvalidOperation):
                pass
        meta = self._catalog.get("_meta", {})
        meta_key = "default_ec2_vcpu_hour_usd"
        try:
            return (
                Decimal(str(meta[meta_key])),
                f"compute_catalog:default:{billing_model}",
                "estimated",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED[billing_model]["vcpu_hour_usd"],
                    f"compute_catalog:hardcoded:{billing_model}",
                    "estimated")

    # ─── K8s pod (default — limits × duration × hourly) ──────────────────

    def _k8s_pod_limits(self, details, window_s):
        if window_s is None or window_s <= 0:
            window_s = Decimal(str(details["duration_ms"])) / _MS_PER_S
        rate, source, confidence = self._resolve_k8s_pod_rate()
        vcpu_count = Decimal(str(details["vcpu_count"]))
        cost = vcpu_count * (window_s / _HOUR_S) * rate
        return ComputeCost(cost, source, confidence)

    def _resolve_k8s_pod_rate(self):
        meta = self._catalog.get("_meta", {})
        try:
            return (
                Decimal(str(meta["default_k8s_pod_vcpu_hour_usd"])),
                "compute_catalog:k8s_pod:limits",
                "computed",
            )
        except (KeyError, InvalidOperation):
            return (_HARDCODED["k8s_pod"]["vcpu_hour_usd"],
                    "compute_catalog:hardcoded:k8s_pod", "estimated")

    # ─── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_rate_block(block, keys):
        return {k: Decimal(str(block[k])) for k in keys}
