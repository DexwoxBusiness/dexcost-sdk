"""cloud_detect — env / DMI / IMDS phases; init never blocks."""

import time
from unittest.mock import patch

from dexcost import cloud_detect
from dexcost.cloud_detect import CloudEnv, detect_now, start_background_detection


_ALL_CLOUD_ENV_VARS = (
    "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
    "AWS_REGION", "AWS_DEFAULT_REGION",
    "ECS_CONTAINER_METADATA_URI_V4", "ECS_CONTAINER_METADATA_URI",
    "WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME",
    "REGION_NAME", "CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX",
    "K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET", "FUNCTION_NAME",
    "FLY_REGION", "FLY_APP_NAME",
    "VERCEL", "VERCEL_REGION", "VERCEL_ENV",
    "MODAL_TASK_ID", "MODAL_FUNCTION_ID", "MODAL_REGION",
    "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "RUNPOD_DC_ID",
    "REPLICATE_MODEL_ID", "REPLICATE_DEPLOYMENT_ID",
    "RENDER", "RENDER_SERVICE_ID", "RENDER_REGION",
    "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_REGION",
    "DYNO", "HEROKU_APP_NAME",
    "KOYEB_SERVICE_NAME", "KOYEB_APP_NAME", "KOYEB_REGION",
    "NETLIFY", "NETLIFY_SITE_ID",
    "CF_PAGES", "CLOUDFLARE_ACCOUNT_ID",
)


def _clear_env(monkeypatch):
    for v in _ALL_CLOUD_ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def _reset_module():
    cloud_detect._result = CloudEnv(None, None, "none")
    cloud_detect._thread = None


def test_aws_lambda_env_resolves_fully(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    env = detect_now()
    assert env.provider == "aws"
    assert env.region == "us-east-1"
    assert env.source == "env"


def test_azure_app_service_provider_no_region(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "x")
    env = detect_now()
    assert env.provider == "azure"
    assert env.region is None
    assert env.source == "env"


def test_gcp_cloud_run_provider_no_region(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "my-svc")
    env = detect_now()
    assert env.provider == "gcp"
    assert env.region is None
    assert env.source == "env"


def test_no_env_no_dmi_returns_undetected(monkeypatch, tmp_path):
    _reset_module()
    _clear_env(monkeypatch)
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(tmp_path / "nope"),)):
        env = detect_now()
    assert env.provider is None
    assert env.region is None
    assert env.source == "none"


def test_dmi_amazon_resolves_provider(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("Amazon EC2\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "aws"
    assert env.source == "dmi"


def test_dmi_google_resolves_provider(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("Google\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "gcp"


def test_dmi_microsoft_resolves_provider(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("Microsoft Corporation\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "azure"


def test_gcp_zone_to_region_strips_trailing_letter():
    from dexcost.cloud_detect import _gcp_zone_to_region
    assert _gcp_zone_to_region("projects/123/zones/us-central1-a") == "us-central1"
    assert _gcp_zone_to_region("us-central1-a") == "us-central1"
    assert _gcp_zone_to_region("") is None


def test_init_never_blocks_when_metadata_unreachable(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    with patch("dexcost.cloud_detect._DMI_PATHS", ()):
        t0 = time.perf_counter()
        start_background_detection()
        elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"init took {elapsed:.3f}s, expected < 50 ms"


def test_track_network_false_skips_probe(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    start_background_detection(track_network=False)
    env = cloud_detect.get_cloud_env()
    assert env.source == "none"
    assert cloud_detect._thread is None


def test_start_with_full_env_does_not_launch_thread(monkeypatch):
    """When env-vars already give provider+region, no Phase 2 thread is needed."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    start_background_detection()
    env = cloud_detect.get_cloud_env()
    assert env.provider == "aws"
    assert env.region == "eu-west-1"
    assert cloud_detect._thread is None


# ── Additions from May-2026 deep-research pass ───────────────────────────


def test_ecs_fargate_metadata_uri_resolves_aws_with_region(monkeypatch):
    """ECS Fargate sets ECS_CONTAINER_METADATA_URI_V4 + AWS_REGION."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4",
        "http://169.254.170.2/v4/metadata-id",
    )
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    env = detect_now()
    assert env.provider == "aws"
    assert env.region == "ap-south-1"
    assert env.source == "env"


def test_ecs_v3_metadata_uri_also_resolves_aws(monkeypatch):
    """Older ECS platform versions set V3 (not V4) metadata URI."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/x")
    env = detect_now()
    assert env.provider == "aws"


def test_azure_container_apps_hostname_yields_region(monkeypatch):
    """Container Apps embeds region in CONTAINER_APP_HOSTNAME — no IMDS needed."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONTAINER_APP_NAME", "my-app")
    monkeypatch.setenv(
        "CONTAINER_APP_HOSTNAME",
        "my-app--abc.proudground-12345.eastus.azurecontainerapps.io",
    )
    env = detect_now()
    assert env.provider == "azure"
    assert env.region == "eastus"
    assert env.source == "env"


def test_azure_container_apps_dns_suffix_yields_region(monkeypatch):
    """Region also parseable out of CONTAINER_APP_ENV_DNS_SUFFIX alone."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONTAINER_APP_NAME", "my-app")
    monkeypatch.setenv(
        "CONTAINER_APP_ENV_DNS_SUFFIX",
        "proudground-12345.westeurope.azurecontainerapps.io",
    )
    env = detect_now()
    assert env.region == "westeurope"


def test_azure_region_name_wins_when_both_present(monkeypatch):
    """When REGION_NAME and CONTAINER_APP_HOSTNAME both exist, REGION_NAME wins."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONTAINER_APP_NAME", "x")
    monkeypatch.setenv("REGION_NAME", "northeurope")
    monkeypatch.setenv(
        "CONTAINER_APP_HOSTNAME",
        "x.y.eastus.azurecontainerapps.io",
    )
    env = detect_now()
    assert env.region == "northeurope"


def test_gcp_k_configuration_alone_signals_gcp(monkeypatch):
    """K_CONFIGURATION is set on Cloud Run regardless of K_SERVICE."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("K_CONFIGURATION", "my-config")
    env = detect_now()
    assert env.provider == "gcp"


def test_bare_aws_region_now_classifies_as_aws(monkeypatch):
    """Bare AWS_REGION is accepted as an AWS signal.

    The dev-laptop-with-AWS-CLI false-positive surface was deemed not worth
    optimizing for; a deployed SDK should attribute. Lambda, ECS, App Runner,
    Beanstalk, and most managed AWS runtimes set AWS_REGION automatically.
    """
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("dexcost.cloud_detect._DMI_PATHS", ()):
        env = detect_now()
    assert env.provider == "aws"
    assert env.region == "us-east-1"


def test_fly_region_env_resolves_provider_and_region(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("FLY_REGION", "iad")
    monkeypatch.setenv("FLY_APP_NAME", "my-app")
    env = detect_now()
    assert env.provider == "fly"
    assert env.region == "iad"
    assert env.source == "env"


def test_fly_app_name_alone_signals_fly(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "my-app")
    env = detect_now()
    assert env.provider == "fly"


def test_vercel_region_resolves_provider_and_region(monkeypatch):
    """Vercel sets both VERCEL and AWS_REGION (it runs on AWS). vercel wins."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_REGION", "iad1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")  # Vercel sets this too
    env = detect_now()
    assert env.provider == "vercel"
    assert env.region == "iad1"


def test_dmi_oraclecloud_resolves_oci(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("OracleCloud.com\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "oci"


def test_dmi_digitalocean_resolves(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "sys_vendor"
    dmi.write_text("DigitalOcean\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "digitalocean"


def test_dmi_hetzner_resolves(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "sys_vendor"
    dmi.write_text("Hetzner\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "hetzner"


def test_dmi_vultr_resolves(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "sys_vendor"
    dmi.write_text("Vultr\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "vultr"


def test_dmi_alibaba_resolves(tmp_path, monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("Alibaba Cloud\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "alibaba"


# ── ML / GPU clouds (zero-egress detection prevents over-attribution) ────


def test_modal_task_id_resolves_modal_with_region(monkeypatch):
    """Modal docs (2026-05) confirm MODAL_TASK_ID + MODAL_REGION."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODAL_TASK_ID", "ta-abc")
    monkeypatch.setenv("MODAL_REGION", "us-east-1")
    env = detect_now()
    assert env.provider == "modal"
    assert env.region == "us-east-1"


def test_runpod_pod_id_resolves_provider(monkeypatch):
    """RunPod docs (2026-05) confirm RUNPOD_POD_ID + RUNPOD_DC_ID."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("RUNPOD_POD_ID", "abc123")
    monkeypatch.setenv("RUNPOD_DC_ID", "US-CA-2")
    env = detect_now()
    assert env.provider == "runpod"
    assert env.region == "US-CA-2"


# ── PaaS app platforms ──────────────────────────────────────────────────


def test_render_resolves(monkeypatch):
    """Render docs (2026-05) confirm RENDER=true. No region env var exists."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-abc")
    env = detect_now()
    assert env.provider == "render"
    assert env.region is None


def test_railway_resolves_with_replica_region(monkeypatch):
    """Railway docs (2026-05) confirm RAILWAY_REPLICA_REGION (NOT RAILWAY_REGION)."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "abc")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", "us-west2")
    env = detect_now()
    assert env.provider == "railway"
    assert env.region == "us-west2"


def test_heroku_dyno_resolves(monkeypatch):
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("DYNO", "web.1")
    env = detect_now()
    assert env.provider == "heroku"


def test_koyeb_resolves(monkeypatch):
    """Koyeb docs (2026-05) confirm KOYEB_APP_NAME + KOYEB_REGION (runtime)."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("KOYEB_APP_NAME", "my-app")
    monkeypatch.setenv("KOYEB_REGION", "fra")
    env = detect_now()
    assert env.provider == "koyeb"
    assert env.region == "fra"


def test_phase2_runs_only_aws_gcp_azure_in_parallel():
    """Phase 2 fanout is the major-3 only — adding 6+ providers would
    lengthen worst-case wait and hit wrong endpoints (DO shares AWS's IP).
    DMI-classified hosts go directly to their provider's probe.
    """
    from dexcost.cloud_detect import _FANOUT_PROBES
    assert _FANOUT_PROBES == ("aws", "gcp", "azure")


def test_phase2_uses_provider_hint_when_dmi_pre_classifies(monkeypatch):
    """When DMI says "oci", _run_probe goes straight to OCI's endpoint —
    no fanout, no AWS IMDS race.
    """
    from dexcost.cloud_detect import _PROBES, _run_probe
    calls: list[str] = []

    def _fake_oci():
        calls.append("oci")
        return cloud_detect.CloudEnv("oci", "us-ashburn-1", "imds")

    def _fake_aws():
        calls.append("aws")
        return None

    monkeypatch.setitem(_PROBES, "oci", _fake_oci)
    monkeypatch.setitem(_PROBES, "aws", _fake_aws)
    env = _run_probe("oci")
    assert env.provider == "oci"
    assert env.region == "us-ashburn-1"
    assert calls == ["oci"]  # AWS probe never fired


def test_ml_cloud_wins_over_underlying_aws(monkeypatch):
    """Modal/RunPod run on AWS — but the platform attribution must win."""
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MODAL_TASK_ID", "ta-abc")
    monkeypatch.setenv("MODAL_REGION", "us-east-1")
    env = detect_now()
    # Modal $0 egress beats AWS $0.09/GB attribution.
    assert env.provider == "modal"
