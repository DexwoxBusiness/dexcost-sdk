"""Compute runtime resolution — env-var cascade + cloud_detect fallback.

Per capture spec §5.5:
  1. Serverless env vars (Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
     Azure Functions, Vercel)
  2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over the underlying VM to
     avoid double-counting)
  3. cloud_detect IaaS (EC2 / GCE / Azure VM)
  4. UNKNOWN
"""

from __future__ import annotations

import pytest


_SERVERLESS_ENV_VARS = (
    "AWS_LAMBDA_FUNCTION_NAME",
    "ECS_CONTAINER_METADATA_URI_V4",
    "ECS_CONTAINER_METADATA_URI",
    "K_SERVICE",
    "FUNCTION_TARGET",
    "FUNCTIONS_WORKER_RUNTIME",
    "VERCEL",
    "KUBERNETES_SERVICE_HOST",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """Strip all runtime-discriminator env vars by default; tests opt in."""
    for var in _SERVERLESS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_lambda_env_wins(monkeypatch):
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    assert resolve_runtime() == RuntimeKind.LAMBDA


def test_fargate_env_wins(monkeypatch):
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    assert resolve_runtime() == RuntimeKind.FARGATE


def test_cloud_run_env_wins(monkeypatch):
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("K_SERVICE", "svc")
    assert resolve_runtime() == RuntimeKind.CLOUD_RUN


def test_cloud_functions_gen2_disambiguated_from_cloud_run(monkeypatch):
    """Cloud Functions Gen2 sets BOTH K_SERVICE and FUNCTION_TARGET."""
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("K_SERVICE", "svc")
    monkeypatch.setenv("FUNCTION_TARGET", "main")
    assert resolve_runtime() == RuntimeKind.CLOUD_FUNCTIONS


def test_azure_functions_env_wins(monkeypatch):
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("FUNCTIONS_WORKER_RUNTIME", "python")
    assert resolve_runtime() == RuntimeKind.AZURE_FUNCTIONS


def test_vercel_env_wins(monkeypatch):
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("VERCEL", "1")
    assert resolve_runtime() == RuntimeKind.VERCEL


def test_k8s_wins_over_aws_iaas(monkeypatch):
    """A pod on EC2 must be billed as k8s_pod, NOT ec2 (avoids double-count)."""
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "aws", "us-east-1", "dmi", instance_type="c7g.xlarge",
        ),
    )
    assert resolve_runtime() == RuntimeKind.K8S_POD


def test_falls_through_to_cloud_detect_ec2(monkeypatch):
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "aws", "us-east-1", "dmi", instance_type="c7g.xlarge",
        ),
    )
    assert resolve_runtime() == RuntimeKind.EC2


def test_falls_through_to_cloud_detect_gce(monkeypatch):
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "gcp", "us-central1", "imds", instance_type="n2-standard-2",
        ),
    )
    assert resolve_runtime() == RuntimeKind.GCE


def test_falls_through_to_cloud_detect_azure_vm(monkeypatch):
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "azure", "eastus", "imds", instance_type="Standard_D2s_v3",
        ),
    )
    assert resolve_runtime() == RuntimeKind.AZURE_VM


def test_undetected_returns_unknown(monkeypatch):
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv(None, None, "none"),
    )
    assert resolve_runtime() == RuntimeKind.UNKNOWN


def test_serverless_wins_over_iaas(monkeypatch):
    """A Lambda always reports as LAMBDA even if cloud_detect resolved AWS."""
    from dexcost import cloud_detect
    from dexcost.compute_runtime import RuntimeKind, resolve_runtime
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "aws", "us-east-1", "dmi", instance_type="c7g.xlarge",
        ),
    )
    assert resolve_runtime() == RuntimeKind.LAMBDA
