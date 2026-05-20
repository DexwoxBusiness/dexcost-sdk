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
# DMI files exposed by Linux at /sys/class/dmi/id/.  Each provider has
# canonical fields per cloud-init's ds-identify; we read all of them and
# apply per-provider matching rules below.
_DMI_FIELDS: tuple[str, ...] = (
    "sys_vendor",
    "board_vendor",
    "product_name",
    "chassis_asset_tag",
    "bios_vendor",
    "product_serial",
)
# Resolved against /sys/class/dmi/id/<field>.  Tuple of (field, needle,
# match_mode, provider) where match_mode is "eq" (case-insensitive equality
# after strip) or "contains" (case-insensitive substring).  Rules are
# transcribed from cloud-init's ds-identify (canonical) plus provider
# documentation, verified May 2026.
_DMI_RULES: tuple[tuple[str, str, str, str], ...] = (
    # ── Canonical signals (chassis_asset_tag / product_name) — these are
    # the per-cloud-init ds-identify fingerprints. Listed FIRST so they
    # win when both a canonical and a backup signal are present on the
    # same host.
    ("chassis_asset_tag", "oraclecloud.com", "eq", "oci"),
    ("chassis_asset_tag", "7783-7084-3265-9085-8269-3286-77", "eq", "azure"),
    ("product_name", "google compute engine", "eq", "gcp"),
    ("product_name", "alibaba cloud ecs", "eq", "alibaba"),

    # ── sys_vendor exact matches — providers whose canonical signal IS
    # sys_vendor per cloud-init.
    ("sys_vendor", "amazon ec2", "eq", "aws"),
    ("sys_vendor", "digitalocean", "eq", "digitalocean"),
    ("sys_vendor", "hetzner", "eq", "hetzner"),
    ("sys_vendor", "vultr", "eq", "vultr"),
    ("sys_vendor", "scaleway", "eq", "scaleway"),
    ("sys_vendor", "microsoft corporation", "eq", "azure"),

    # ── Looser substring backups — older hypervisor generations whose
    # exact sys_vendor varies. Listed LAST.
    ("sys_vendor", "amazon", "contains", "aws"),
    ("sys_vendor", "google", "contains", "gcp"),
    ("sys_vendor", "alibaba cloud", "contains", "alibaba"),
    ("sys_vendor", "ovh", "contains", "ovh"),
)
_DMI_PATHS: tuple[str, ...] = tuple(
    f"/sys/class/dmi/id/{field}" for field in _DMI_FIELDS
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
    # ── GPU / ML clouds (zero egress) ──────────────────────────────────
    # Detection prevents the universal $0.09/GB default from over-attributing
    # on platforms whose marketing point is $0 egress.
    #
    # All env-var names below are confirmed against each provider's official
    # 2026-05 docs — not pattern-matched or guessed.

    # Modal (modal.com/docs/guide/environment_variables): MODAL_TASK_ID is
    # set in every container; MODAL_REGION carries the underlying cloud's
    # region code (us-east-1 / us-central1 / us-ashburn-1).
    if os.environ.get("MODAL_TASK_ID") or os.environ.get("MODAL_IMAGE_ID"):
        return CloudEnv("modal", os.environ.get("MODAL_REGION") or None, "env")

    # RunPod (docs.runpod.io/pods/templates/environment-variables): pod-id,
    # hostname, and data-centre id are all auto-injected.
    if os.environ.get("RUNPOD_POD_ID") or os.environ.get("RUNPOD_POD_HOSTNAME"):
        return CloudEnv("runpod", os.environ.get("RUNPOD_DC_ID") or None, "env")

    # ── PaaS app platforms ──────────────────────────────────────────────
    # Render (render.com/docs/environment-variables): RENDER=true and a
    # collection of RENDER_* IDs. No region env var is documented.
    if os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"):
        return CloudEnv("render", None, "env")

    # Railway (docs.railway.com/reference/variables): RAILWAY_PROJECT_ID +
    # RAILWAY_REPLICA_REGION (e.g. "us-west2"). Note: NOT "RAILWAY_REGION".
    if (
        os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_ENVIRONMENT_ID")
    ):
        return CloudEnv(
            "railway",
            os.environ.get("RAILWAY_REPLICA_REGION") or None,
            "env",
        )

    # Heroku: DYNO ("web.1" / "worker.1" / "scheduler.x") is the long-standing
    # de-facto detection signal — present on every dyno.
    if os.environ.get("DYNO"):
        return CloudEnv("heroku", None, "env")

    # Koyeb (koyeb.com/docs/build-and-deploy/environment-variables):
    # KOYEB_APP_NAME / KOYEB_SERVICE_NAME (build+runtime) plus KOYEB_REGION
    # (runtime only).
    if os.environ.get("KOYEB_SERVICE_NAME") or os.environ.get("KOYEB_APP_NAME"):
        return CloudEnv("koyeb", os.environ.get("KOYEB_REGION") or None, "env")

    # ── Fly.io (fly.io/docs/machines/runtime-environment) ───────────────
    # FLY_REGION (3-letter: ams, iad, lhr, …), FLY_APP_NAME, FLY_MACHINE_ID.
    if os.environ.get("FLY_REGION") or os.environ.get("FLY_APP_NAME"):
        return CloudEnv("fly", os.environ.get("FLY_REGION") or None, "env")

    # ── Vercel (vercel.com/docs/projects/environment-variables) ────────
    # VERCEL=1, VERCEL_ENV, VERCEL_REGION (e.g. "iad1", "sfo1"). Vercel ALSO
    # exports AWS_REGION mapped to the underlying AWS region (sfo1 →
    # us-west-1); detecting vercel first surfaces vercel-tier attribution.
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_REGION"):
        return CloudEnv("vercel", os.environ.get("VERCEL_REGION") or None, "env")

    # Note: Replicate, Netlify Functions, and Cloudflare Workers were
    # researched but excluded from env-var detection — Replicate has no
    # documented runtime detection var, Netlify's NETLIFY=true is build-time
    # only (functions run on AWS Lambda and surface as AWS), and Cloudflare
    # Workers pass env via the request `env` parameter rather than the
    # process environment by default. They remain in the egress catalog
    # ($0 rate) for future detection paths.

    # ── AWS ─────────────────────────────────────────────────────────────
    # Lambda / Execution-Env / ECS signals are definitive.  AWS_REGION alone
    # is also a strong signal — set on Lambda, ECS (Fargate / on-EC2), App
    # Runner, Beanstalk, and most managed runtimes.  A bare-laptop dev with
    # AWS CLI configured is the only false-positive surface, and it's their
    # responsibility — a deployed SDK should attribute.
    if (
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("AWS_EXECUTION_ENV")
        or os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    ):
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
    # Cloud Functions Gen2.  No region env var exists; Phase 2 metadata
    # probe resolves it.  GOOGLE_CLOUD_PROJECT is set by App Engine / Cloud
    # Functions Gen1 / many CI runners — treat as a positive provider
    # signal only when paired with a Cloud-Run-style marker, otherwise too
    # noisy (gcloud CLI exports it on a dev laptop).
    if any(os.environ.get(k) for k in
           ("K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET",
            "FUNCTION_NAME")):
        return CloudEnv("gcp", None, "env")
    return None


# ---------------------------------------------------------------------------
# Phase 1b — DMI check
# ---------------------------------------------------------------------------


def _read_dmi() -> dict[str, str]:
    """Read all DMI fields we care about; missing files are silently skipped."""
    result: dict[str, str] = {}
    for field in _DMI_FIELDS:
        try:
            with open(f"/sys/class/dmi/id/{field}", encoding="utf-8") as f:
                result[field] = f.read().strip().lower()
        except OSError:
            continue
    return result


def _detect_dmi() -> CloudEnv | None:
    """Resolve the cloud provider from DMI fields.

    Rules are ordered from most specific to most generic; the first match
    wins. The canonical signals (chassis_asset_tag, product_name) take
    precedence over sys_vendor where both are documented (per cloud-init's
    ds-identify).
    """
    dmi = _read_dmi()
    for field, needle, mode, provider in _DMI_RULES:
        value = dmi.get(field, "")
        if not value:
            continue
        if mode == "eq" and value == needle:
            return CloudEnv(provider, None, "dmi")
        if mode == "contains" and needle in value:
            return CloudEnv(provider, None, "dmi")
    return None


# ---------------------------------------------------------------------------
# Phase 2 — metadata probes
# ---------------------------------------------------------------------------


def _gcp_path_to_region(value: str, drop_zone_letter: bool) -> str | None:
    """Strip GCP metadata-server response to a bare region.

    Both ``/instance/zone`` (returns ``projects/PROJECT/zones/us-central1-a``)
    and ``/instance/region`` (returns ``projects/PROJECT/regions/us-central1``)
    use the same ``projects/.../X/<name>`` shape. ``drop_zone_letter`` strips
    the trailing ``-<letter>`` from the zone form to yield a region.
    """
    if not value:
        return None
    last = value.rsplit("/", 1)[-1]
    if not last:
        return None
    if drop_zone_letter:
        if "-" not in last:
            return None
        return last.rsplit("-", 1)[0]
    return last


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
    # Try /region first — works on Cloud Run / Cloud Functions Gen2 (which
    # return a placeholder on /zone) and on GCE VMs (where /zone exists but
    # /region also returns projects/.../regions/<region>).  Fall back to
    # /zone for the rare case where /region is unavailable.
    headers = {"Metadata-Flavor": "Google"}
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/region",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            region_path = resp.read().decode("ascii").strip()
        region = _gcp_path_to_region(region_path, drop_zone_letter=False)
        if region:
            return CloudEnv("gcp", region, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        pass

    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            zone = resp.read().decode("ascii").strip()
        return CloudEnv("gcp", _gcp_path_to_region(zone, drop_zone_letter=True), "imds")
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


def _probe_oci() -> CloudEnv | None:
    # OCI IMDSv2 (canonical per docs.oracle.com/.../gettingmetadata.htm):
    # base http://169.254.169.254/opc/v2/instance/ + "Authorization: Bearer Oracle".
    # /region returns abbreviated codes (phx, iad) for some regions and full
    # codes (eu-frankfurt-1) for others — /canonicalRegionName always returns
    # the FULL identifier (us-phoenix-1, us-ashburn-1, eu-frankfurt-1), which
    # is what our egress catalog keys are.
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/opc/v2/instance/canonicalRegionName",
            headers={"Authorization": "Bearer Oracle"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            region = resp.read().decode("ascii").strip().lower()
        return CloudEnv("oci", region or None, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_digitalocean() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/v1/region",
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            region = resp.read().decode("ascii").strip().lower()
        return CloudEnv("digitalocean", region or None, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_alibaba() -> CloudEnv | None:
    # Alibaba ECS metadata at 100.100.100.200 — different IP.  IMDSv2 PUT
    # token flow exists; v1 still works on most images.
    try:
        req = urllib.request.Request(
            "http://100.100.100.200/latest/meta-data/region-id",
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            region = resp.read().decode("ascii").strip().lower()
        return CloudEnv("alibaba", region or None, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


# Provider hint → metadata probe.  When provider is already known via DMI
# we go straight to its probe; otherwise we fan out the major three
# (aws/gcp/azure) in parallel — adding OCI/DO/Alibaba to the parallel set
# would lengthen the worst-case wait and hit the wrong metadata server
# on AWS (DO uses the same 169.254.169.254 IP), so they only run when DMI
# pre-classifies the host.
_PROBES = {
    "aws": _probe_aws,
    "gcp": _probe_gcp,
    "azure": _probe_azure,
    "oci": _probe_oci,
    "digitalocean": _probe_digitalocean,
    "alibaba": _probe_alibaba,
}
_FANOUT_PROBES = ("aws", "gcp", "azure")


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
        threading.Thread(target=_runner, args=(_PROBES[name],), daemon=True)
        for name in _FANOUT_PROBES
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
