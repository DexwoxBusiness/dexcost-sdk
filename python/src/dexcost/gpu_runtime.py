"""Active GPU runtime resolver.

Sibling of :mod:`dexcost.compute_runtime`. Coexists without modification —
``compute_runtime`` answers "which compute billing model" and
``gpu_runtime`` answers "which GPU billing model (if any)".

Cascade priority (capture spec §5.5):

1. **Serverless GPU env vars** — ``MODAL_TASK_ID`` / ``RUNPOD_POD_ID`` /
   ``REPLICATE_MODEL`` win immediately when NVML is available.
2. **IaaS GPU via cloud_detect** — when ``CloudEnv.provider`` resolves to
   AWS / GCP / Azure AND the ``instance_type`` matches a GPU family regex
   AND NVML reports ≥1 device, classify as ``AWS_EC2_GPU`` /
   ``GCP_GCE_BUNDLED`` / ``GCP_GCE_N1_ATTACHED`` (Decision #9) /
   ``AZURE_VM_GPU`` / ``AZURE_VM_VGPU`` (Decision #10).
3. **Reserved-GPU providers** — Lambda Labs / CoreWeave when cloud_detect
   resolves them AND NVML reports ≥1 device.
4. **NONE** when NVML isn't available, reports 0 devices, or the runtime
   isn't on the v1 covered list (Decision #5 — NVIDIA only).
"""

from __future__ import annotations

import os
import re
from enum import Enum

from dexcost import cloud_detect, nvml_reader


class GpuRuntimeKind(str, Enum):
    """Active GPU runtime; controls billing-model dispatch in the pricing engine."""

    MODAL = "modal"
    RUNPOD = "runpod"
    REPLICATE = "replicate"
    LAMBDA_LABS = "lambda_labs"
    COREWEAVE = "coreweave"
    AWS_EC2_GPU = "aws_ec2_gpu"
    GCP_GCE_BUNDLED = "gcp_gce_bundled"
    GCP_GCE_N1_ATTACHED = "gcp_gce_n1_attached"
    AZURE_VM_GPU = "azure_vm_gpu"
    AZURE_VM_VGPU = "azure_vm_vgpu"
    NONE = "none"


# ─── GPU instance-family matchers ────────────────────────────────────────────

# AWS GPU EC2 families per 2026: g4/g4dn/g5/g5g/g6/g6e/p3/p4d/p4de/p5/p5e/p5en.
_AWS_GPU_FAMILY_RE = re.compile(
    r"^(g4|g4dn|g5|g5g|g6|g6e|p3|p4d|p4de|p5|p5e|p5en)\.",
    re.IGNORECASE,
)

# GCP A2/A3 (bundled GPU instances) + G2 (L4-bundled).
_GCP_BUNDLED_GPU_FAMILY_RE = re.compile(
    r"^(a2|a3|a4|g2)-",
    re.IGNORECASE,
)

# GCP N1 — attached-accelerator path per Decision #9 (no metadata endpoint
# exposes the accelerator type; rely on NVML fallback).
_GCP_N1_FAMILY_RE = re.compile(r"^n1-", re.IGNORECASE)

# Azure ND/NC series — bundled GPU instances.
_AZURE_GPU_FAMILY_RE = re.compile(
    r"^Standard_(ND|NC)",
    re.IGNORECASE,
)

# Azure NVadsA10 v5 — fractional vGPU profiles per Decision #10.
_AZURE_VGPU_FAMILY_RE = re.compile(
    r"^Standard_NV\d+ads_A10_v5",
    re.IGNORECASE,
)


def _is_aws_gpu_instance(instance_type: str | None) -> bool:
    return bool(instance_type and _AWS_GPU_FAMILY_RE.match(instance_type))


def _is_gcp_bundled_gpu_instance(instance_type: str | None) -> bool:
    return bool(instance_type and _GCP_BUNDLED_GPU_FAMILY_RE.match(instance_type))


def _is_gcp_n1_instance(instance_type: str | None) -> bool:
    return bool(instance_type and _GCP_N1_FAMILY_RE.match(instance_type))


def _is_azure_vgpu_instance(instance_type: str | None) -> bool:
    return bool(instance_type and _AZURE_VGPU_FAMILY_RE.match(instance_type))


def _is_azure_gpu_instance(instance_type: str | None) -> bool:
    return bool(instance_type and _AZURE_GPU_FAMILY_RE.match(instance_type))


# ─── Resolver ────────────────────────────────────────────────────────────────


def resolve_gpu_runtime() -> GpuRuntimeKind:
    """Return the active GPU runtime, or ``NONE`` when there's no GPU.

    The cascade short-circuits on the FIRST positive match. If NVML can't
    initialize or reports 0 devices, returns ``NONE`` regardless of env-var
    signals — a Modal task on a CPU-only Modal function emits no GPU events.
    """
    # NVML must be available AND see ≥1 device for any GPU event emission.
    if not nvml_reader.nvml_available():
        return GpuRuntimeKind.NONE
    device_count = nvml_reader.get_device_count()
    if not device_count:
        return GpuRuntimeKind.NONE

    # 1. Serverless GPU env vars — fastest path, decisive when set.
    if os.environ.get("MODAL_TASK_ID") or os.environ.get("MODAL_IMAGE_ID"):
        return GpuRuntimeKind.MODAL
    if os.environ.get("RUNPOD_POD_ID") or os.environ.get("RUNPOD_POD_HOSTNAME"):
        return GpuRuntimeKind.RUNPOD
    if os.environ.get("REPLICATE_MODEL") or os.environ.get("REPLICATE_PREDICTION_ID"):
        return GpuRuntimeKind.REPLICATE

    # 2/3. Fall through to cloud_detect for IaaS + reserved-GPU providers.
    env = cloud_detect.get_cloud_env()
    provider = env.provider
    instance_type = env.instance_type

    if provider == "lambda_labs":
        return GpuRuntimeKind.LAMBDA_LABS
    if provider == "coreweave":
        return GpuRuntimeKind.COREWEAVE

    if provider == "aws":
        if _is_aws_gpu_instance(instance_type):
            return GpuRuntimeKind.AWS_EC2_GPU

    if provider == "gcp":
        if _is_gcp_bundled_gpu_instance(instance_type):
            return GpuRuntimeKind.GCP_GCE_BUNDLED
        if _is_gcp_n1_instance(instance_type):
            # Decision #9 — N1 + attached accelerator detected via NVML only.
            return GpuRuntimeKind.GCP_GCE_N1_ATTACHED

    if provider == "azure":
        # vGPU first — more specific regex than the broader ND/NC matcher.
        if _is_azure_vgpu_instance(instance_type):
            return GpuRuntimeKind.AZURE_VM_VGPU
        if _is_azure_gpu_instance(instance_type):
            return GpuRuntimeKind.AZURE_VM_GPU

    # No matching runtime → no GPU events.
    return GpuRuntimeKind.NONE
