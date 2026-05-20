"""cloud_detect — env / DMI / IMDS phases; init never blocks."""

import time
from unittest.mock import patch

from dexcost import cloud_detect
from dexcost.cloud_detect import CloudEnv, detect_now, start_background_detection


_ALL_CLOUD_ENV_VARS = (
    "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
    "AWS_REGION", "AWS_DEFAULT_REGION",
    "WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME",
    "REGION_NAME", "K_SERVICE", "GAE_ENV", "FUNCTION_TARGET",
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


def test_bare_aws_region_alone_is_not_enough(monkeypatch):
    """A developer laptop with AWS_REGION exported should NOT be classified aws.

    AWS_REGION is meaningful only alongside a Lambda/ECS/ExecutionEnv signal.
    """
    _reset_module()
    _clear_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("dexcost.cloud_detect._DMI_PATHS", ()):
        env = detect_now()
    assert env.provider is None
    assert env.source == "none"
