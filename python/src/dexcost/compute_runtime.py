"""Active compute-runtime resolver.

Cascade priority (capture spec §5.5):

  1. Serverless env vars — Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
     Azure Functions, Vercel
  2. ``KUBERNETES_SERVICE_HOST`` → k8s_pod (wins over the underlying VM so a
     pod-on-EC2 is billed once as k8s_pod, not twice as k8s_pod + ec2)
  3. cloud_detect IaaS fallback — EC2 / GCE / Azure VM via the existing
     ``CloudEnv.provider`` resolved by ``dexcost.cloud_detect``
  4. UNKNOWN

The discriminator value emitted on ``compute_cost`` events
(``details.billing_model``) is derived from this enum in
``compute_accountant._billing_model_for``.
"""

from __future__ import annotations

import os
from enum import Enum

from dexcost import cloud_detect


class RuntimeKind(str, Enum):
    LAMBDA = "lambda"
    FARGATE = "fargate"
    EC2 = "ec2"
    CLOUD_RUN = "cloud_run"
    CLOUD_FUNCTIONS = "cloud_functions"
    GCE = "gce"
    AZURE_FUNCTIONS = "azure_functions"
    AZURE_VM = "azure_vm"
    VERCEL = "vercel_fluid"
    K8S_POD = "k8s_pod"
    UNKNOWN = "unknown"


def resolve_runtime() -> RuntimeKind:
    """Return the active compute runtime for the current process."""
    # 1. Serverless env vars take highest priority — a Lambda is a Lambda
    #    even though it also runs on AWS infrastructure.
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return RuntimeKind.LAMBDA
    if (
        os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
    ):
        return RuntimeKind.FARGATE
    if os.environ.get("K_SERVICE"):
        # Cloud Functions Gen2 sets BOTH K_SERVICE and FUNCTION_TARGET; plain
        # Cloud Run sets only K_SERVICE. Distinguish so downstream dashboards
        # can break out function-vs-service even though the billing math is
        # identical (Cloud Functions Gen2 IS Cloud Run under the hood).
        if os.environ.get("FUNCTION_TARGET"):
            return RuntimeKind.CLOUD_FUNCTIONS
        return RuntimeKind.CLOUD_RUN
    if os.environ.get("FUNCTIONS_WORKER_RUNTIME"):
        return RuntimeKind.AZURE_FUNCTIONS
    if os.environ.get("VERCEL"):
        return RuntimeKind.VERCEL

    # 2. Kubernetes wins over the underlying VM. A pod on EC2 reports as
    #    k8s_pod (billed at pod-limits × duration); the EC2 instance share
    #    would double-count the same compute hour.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return RuntimeKind.K8S_POD

    # 3. Fall through to cloud_detect IaaS classification.
    env = cloud_detect.get_cloud_env()
    if env.provider == "aws":
        return RuntimeKind.EC2
    if env.provider == "gcp":
        return RuntimeKind.GCE
    if env.provider == "azure":
        return RuntimeKind.AZURE_VM

    return RuntimeKind.UNKNOWN
