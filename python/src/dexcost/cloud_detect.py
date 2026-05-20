"""Cloud-environment detection for egress pricing.

Phase 1a — env-var detection (sub-millisecond, synchronous).
Phase 1b — DMI vendor check (~1 ms, Linux-only).
Phase 2  — background metadata probe (daemon thread, ~250 ms budget,
           never blocks ``dexcost.init()``).

See spec §5 for the resolution rules per provider.

Notes — research May 2026:

- AWS Lambda / Fargate / App Runner set ``AWS_REGION`` automatically; ECS
  (Fargate and on-EC2) also sets ``ECS_CONTAINER_METADATA_URI_V4``. The Lambda
  / Execution-Env / ECS signals are treated as definitive "this is AWS"
  markers; bare ``AWS_REGION`` is only used to fill in the region when one of
  those markers is also present (a developer laptop may set ``AWS_REGION`` for
  the AWS CLI without actually running on AWS).
- Azure Container Apps embeds the region in ``CONTAINER_APP_HOSTNAME`` and
  ``CONTAINER_APP_ENV_DNS_SUFFIX`` as ``<host>.<REGION>.azurecontainerapps.io``;
  the env-var phase parses it out for free, no IMDS round-trip needed.
- GCP Cloud Run / Cloud Functions Gen2 / App Engine do NOT expose a region
  env var; region must come from the metadata server (Phase 2). ``K_SERVICE``
  / ``K_REVISION`` / ``K_CONFIGURATION`` are reserved auto-set markers.
- AWS IMDSv2 has a default HTTP hop-limit of 1, which prevents Docker/Pod
  containers (other than EKS managed node groups, which default to hop-limit=2)
  from reaching the metadata service. ``_probe_aws`` fails-silent in that
  case and detection falls through to Tier 3 of the pricing degradation
  ladder. The SDK cannot raise the hop-limit from inside the container.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 0.25  # seconds — bounds Phase 2 wall time
_DMI_PATHS: tuple[str, ...] = (
    "/sys/class/dmi/id/board_vendor",
    "/sys/class/dmi/id/sys_vendor",
)


@dataclass(frozen=True)
class CloudEnv:
    """Detected cloud environment.

    ``source`` is the audit trail: ``"env" | "dmi" | "imds" | "none"``.
    """

    provider: str | None
    region: str | None
    source: str


_lock = threading.Lock()
_result: CloudEnv = CloudEnv(None, None, "none")
_thread: threading.Thread | None = None


def get_cloud_env() -> CloudEnv:
    """Return the most recently resolved CloudEnv (may be ``source='none'``)."""
    with _lock:
        return _result


def _set_result(env: CloudEnv) -> None:
    global _result
    with _lock:
        _result = env


# ---------------------------------------------------------------------------
# Phase 1a — environment variable detection
# ---------------------------------------------------------------------------


_AZ_CA_REGION_RE = re.compile(
    r"\.([a-z0-9-]+)\.azurecontainerapps\.io$", re.IGNORECASE,
)


def _azure_container_apps_region() -> str | None:
    """Parse the Azure region out of a Container Apps hostname/DNS suffix.

    Both CONTAINER_APP_HOSTNAME and CONTAINER_APP_ENV_DNS_SUFFIX are formatted
    as ``<...>.<REGION>.azurecontainerapps.io`` (verified May 2026).
    """
    for var in ("CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX"):
        value = os.environ.get(var, "")
        match = _AZ_CA_REGION_RE.search(value)
        if match:
            return match.group(1).lower()
    return None


def _detect_env() -> CloudEnv | None:
    # ── AWS ─────────────────────────────────────────────────────────────
    # Lambda / managed runtimes set AWS_EXECUTION_ENV; ECS (Fargate or
    # on-EC2) sets ECS_CONTAINER_METADATA_URI_V4 (V4 always, V3 on older
    # platform versions).  Either is a definitive "this is AWS" marker.
    is_aws = bool(
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("AWS_EXECUTION_ENV")
        or os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
    )
    if is_aws:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return CloudEnv("aws", region, "env")

    # ── Azure ───────────────────────────────────────────────────────────
    if any(os.environ.get(k) for k in
           ("WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME")):
        # Container Apps embeds region in CONTAINER_APP_HOSTNAME / DNS suffix;
        # App Service may set REGION_NAME (best-effort, not guaranteed).
        region = (
            os.environ.get("REGION_NAME")
            or _azure_container_apps_region()
            or None
        )
        return CloudEnv("azure", region, "env")

    # ── GCP ─────────────────────────────────────────────────────────────
    # K_SERVICE / K_REVISION / K_CONFIGURATION are reserved by Cloud Run +
    # Cloud Functions Gen2 (built on Cloud Run).  No region env var exists;
    # Phase 2 metadata probe resolves it.
    if any(os.environ.get(k) for k in
           ("K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET")):
        return CloudEnv("gcp", None, "env")
    return None


# ---------------------------------------------------------------------------
# Phase 1b — DMI check
# ---------------------------------------------------------------------------


def _detect_dmi() -> CloudEnv | None:
    for path in _DMI_PATHS:
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip().lower()
        except OSError:
            continue
        if "amazon" in value:
            return CloudEnv("aws", None, "dmi")
        if "google" in value:
            return CloudEnv("gcp", None, "dmi")
        if "microsoft" in value:
            return CloudEnv("azure", None, "dmi")
    return None


# ---------------------------------------------------------------------------
# Phase 2 — metadata probes
# ---------------------------------------------------------------------------


def _gcp_zone_to_region(zone: str) -> str | None:
    if not zone:
        return None
    last = zone.rsplit("/", 1)[-1]
    if "-" not in last:
        return None
    return last.rsplit("-", 1)[0]


def _probe_aws() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            token = resp.read().decode("ascii")
        req2 = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/placement/region",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(req2, timeout=_PROBE_TIMEOUT) as resp:
            region = resp.read().decode("ascii").strip()
        return CloudEnv("aws", region or None, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_gcp() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            zone = resp.read().decode("ascii").strip()
        return CloudEnv("gcp", _gcp_zone_to_region(zone), "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_azure() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        region = payload.get("compute", {}).get("location") or None
        return CloudEnv("azure", region, "imds")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


_PROBES = {"aws": _probe_aws, "gcp": _probe_gcp, "azure": _probe_azure}


def _run_probe(provider_hint: str | None) -> CloudEnv:
    """Run Phase 2 probes; return the first success, or "none"."""
    if provider_hint and provider_hint in _PROBES:
        env = _PROBES[provider_hint]()
        return env if env is not None else CloudEnv(provider_hint, None, "imds")

    results: list[CloudEnv] = []
    done = threading.Event()
    lock = threading.Lock()

    def _runner(fn):  # type: ignore[no-untyped-def]
        env = fn()
        if env is not None:
            with lock:
                if not results:
                    results.append(env)
                    done.set()

    threads = [
        threading.Thread(target=_runner, args=(fn,), daemon=True)
        for fn in _PROBES.values()
    ]
    for t in threads:
        t.start()
    done.wait(timeout=_PROBE_TIMEOUT + 0.05)
    if results:
        return results[0]
    return CloudEnv(None, None, "none")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def detect_now() -> CloudEnv:
    """Run Phase 1a + 1b synchronously. Used by tests; never calls IMDS."""
    env = _detect_env()
    if env is not None and env.provider is not None and env.region is not None:
        return env
    env_dmi = _detect_dmi()
    if env is None:
        env = env_dmi
    return env if env is not None else CloudEnv(None, None, "none")


def start_background_detection(track_network: bool = True) -> None:
    """Resolve provider/region without blocking. Idempotent.

    When ``track_network`` is False, no probe is launched.
    """
    global _thread
    if not track_network:
        _set_result(CloudEnv(None, None, "none"))
        return

    initial = detect_now()
    _set_result(initial)
    if initial.provider is not None and initial.region is not None:
        return

    def _background() -> None:
        try:
            env = _run_probe(initial.provider)
            if env.provider is not None:
                if initial.region and not env.region:
                    env = CloudEnv(env.provider, initial.region, env.source)
                _set_result(env)
        except Exception:  # noqa: BLE001 — fail-silent
            _log.warning("cloud_detect background probe failed", exc_info=True)

    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(
        target=_background, daemon=True, name="dexcost-cloud-detect"
    )
    _thread.start()
