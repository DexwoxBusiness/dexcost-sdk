"""GPU runtime cascade — serverless env > IaaS GPU family > NVML presence.

Sibling of compute_runtime.py; coexists without modification.
"""

from __future__ import annotations

import pytest

from dexcost.cloud_detect import CloudEnv


# Scrub env vars that would otherwise leak between tests.
_SERVERLESS_VARS = (
    "MODAL_TASK_ID", "MODAL_IMAGE_ID",
    "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME",
    "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for v in _SERVERLESS_VARS:
        monkeypatch.delenv(v, raising=False)


# ─── Serverless GPU clouds (env-var detection wins) ──────────────────────────

def test_modal_env_wins(monkeypatch):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setenv("MODAL_TASK_ID", "task-abc")
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("modal", None, "env"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.MODAL


def test_runpod_env_wins(monkeypatch):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-abc")
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("runpod", None, "env"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.RUNPOD


def test_replicate_env_wins(monkeypatch):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setenv("REPLICATE_MODEL", "owner/model")
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("replicate", None, "env"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.REPLICATE


# ─── IaaS GPU via cloud_detect + GPU-family regex ────────────────────────────

@pytest.mark.parametrize("instance_type,expected", [
    # AWS GPU families — see plan Task 3 regex
    ("p5.48xlarge",     "aws_ec2_gpu"),
    ("p4d.24xlarge",    "aws_ec2_gpu"),
    ("p4de.24xlarge",   "aws_ec2_gpu"),
    ("p5e.48xlarge",    "aws_ec2_gpu"),
    ("p5en.48xlarge",   "aws_ec2_gpu"),
    ("p3.2xlarge",      "aws_ec2_gpu"),
    ("g4dn.xlarge",     "aws_ec2_gpu"),
    ("g4dn.metal",      "aws_ec2_gpu"),
    ("g5.xlarge",       "aws_ec2_gpu"),
    ("g5g.xlarge",      "aws_ec2_gpu"),
    ("g6.xlarge",       "aws_ec2_gpu"),
    ("g6e.xlarge",      "aws_ec2_gpu"),
    # NOT GPU
    ("c7g.xlarge",      "none"),
    ("t3.medium",       "none"),
    ("m7i.large",       "none"),
])
def test_aws_gpu_family_detection(monkeypatch, instance_type, expected):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr(
        "dexcost.gpu_runtime.nvml_reader.get_device_count",
        lambda: (1 if expected != "none" else 0),
    )
    monkeypatch.setattr(
        "dexcost.gpu_runtime.cloud_detect.get_cloud_env",
        lambda: CloudEnv("aws", "us-east-1", "imds", instance_type=instance_type),
    )
    result = resolve_gpu_runtime()
    if expected == "none":
        assert result == GpuRuntimeKind.NONE
    else:
        assert result == GpuRuntimeKind.AWS_EC2_GPU


@pytest.mark.parametrize("instance_type,expected", [
    ("a2-highgpu-1g",     "gcp_gce_bundled"),
    ("a2-ultragpu-1g",    "gcp_gce_bundled"),
    ("a3-highgpu-8g",     "gcp_gce_bundled"),
    ("a3-edgegpu-8g",     "gcp_gce_bundled"),
    ("g2-standard-4",     "gcp_gce_bundled"),
    # N1 (CPU family) — falls through to N1+attached-accelerator per Decision #9
    ("n1-standard-8",     "gcp_gce_n1_attached"),
    ("n1-highmem-4",      "gcp_gce_n1_attached"),
    # No GPU when NVML reports zero (e.g. e2-standard with no accelerator)
    ("e2-standard-4",     "none"),
])
def test_gcp_gpu_family_detection(monkeypatch, instance_type, expected):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    # NVML reports >=1 device unless we're in the e2 (CPU-only) test.
    monkeypatch.setattr(
        "dexcost.gpu_runtime.nvml_reader.get_device_count",
        lambda: (1 if expected != "none" else 0),
    )
    monkeypatch.setattr(
        "dexcost.gpu_runtime.cloud_detect.get_cloud_env",
        lambda: CloudEnv("gcp", "us-central1", "imds", instance_type=instance_type),
    )
    result = resolve_gpu_runtime()
    if expected == "none":
        assert result == GpuRuntimeKind.NONE
    else:
        assert result.value == expected


@pytest.mark.parametrize("instance_type,expected", [
    # Azure ND/NC series — instance GPU
    ("Standard_ND96isr_H100_v5",       "azure_vm_gpu"),
    ("Standard_ND96amsr_A100_v4",      "azure_vm_gpu"),
    ("Standard_ND96asr_v4",            "azure_vm_gpu"),
    ("Standard_NC24ads_A100_v4",       "azure_vm_gpu"),
    ("Standard_NC6s_v3",               "azure_vm_gpu"),
    # Azure NVadsA10 v5 — fractional vGPU (Decision #10)
    ("Standard_NV6ads_A10_v5",         "azure_vm_vgpu"),
    ("Standard_NV12ads_A10_v5",        "azure_vm_vgpu"),
    ("Standard_NV36ads_A10_v5",        "azure_vm_vgpu"),
    ("Standard_NV72ads_A10_v5",        "azure_vm_vgpu"),
    # Non-GPU
    ("Standard_D2s_v3",                "none"),
    ("Standard_B2ms",                  "none"),
])
def test_azure_gpu_family_detection(monkeypatch, instance_type, expected):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr(
        "dexcost.gpu_runtime.nvml_reader.get_device_count",
        lambda: (1 if expected != "none" else 0),
    )
    monkeypatch.setattr(
        "dexcost.gpu_runtime.cloud_detect.get_cloud_env",
        lambda: CloudEnv("azure", "eastus", "imds", instance_type=instance_type),
    )
    result = resolve_gpu_runtime()
    if expected == "none":
        assert result == GpuRuntimeKind.NONE
    else:
        assert result.value == expected


# ─── Reserved-GPU providers (Lambda Labs / CoreWeave) ────────────────────────

def test_lambda_labs_via_cloud_detect(monkeypatch):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 8)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("lambda_labs", None, "dmi"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.LAMBDA_LABS


def test_coreweave_via_cloud_detect(monkeypatch):
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 8)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("coreweave", None, "dmi"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.COREWEAVE


# ─── No NVML / no GPU → NONE ─────────────────────────────────────────────────

def test_nvml_unavailable_returns_none(monkeypatch):
    """Even on a Modal task env, no NVML → no GPU events emitted."""
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setenv("MODAL_TASK_ID", "x")
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: False)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("modal", None, "env"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.NONE


def test_nvml_zero_devices_returns_none(monkeypatch):
    """NVML loads but reports 0 devices → no events."""
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 0)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv("aws", "us-east-1", "imds",
                                          instance_type="p5.48xlarge"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.NONE


def test_undetected_provider_returns_none(monkeypatch):
    """No cloud provider resolved AND no GPU env vars → NONE."""
    from dexcost.gpu_runtime import GpuRuntimeKind, resolve_gpu_runtime
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: True)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: 0)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env",
                        lambda: CloudEnv(None, None, "none"))
    assert resolve_gpu_runtime() == GpuRuntimeKind.NONE
