//! Active GPU runtime resolver — Phase 2 Task 3.
//!
//! Sibling of [`crate::core::compute_runtime`]. Coexists without
//! modification — `compute_runtime` answers "which compute billing model"
//! and `gpu_runtime` answers "which GPU billing model (if any)".
//!
//! Cascade priority (capture spec §5.5):
//!
//! 1. **Serverless GPU env vars** — `MODAL_TASK_ID` / `RUNPOD_POD_ID` /
//!    `REPLICATE_MODEL` win immediately when NVML is available.
//! 2. **IaaS GPU via cloud_detect** — when `CloudEnv.provider` resolves
//!    to AWS / GCP / Azure AND `instance_type` matches a GPU family regex
//!    AND NVML reports ≥1 device, classify as `AwsEc2Gpu` /
//!    `GcpGceBundled` / `GcpGceN1Attached` (Decision #9) / `AzureVmGpu` /
//!    `AzureVmVgpu` (Decision #10).
//! 3. **Reserved-GPU providers** — Lambda Labs / CoreWeave when
//!    cloud_detect resolves them AND NVML reports ≥1 device.
//! 4. **None** when NVML isn't available, reports 0 devices, or the
//!    runtime isn't on the v1 covered list (Decision #5 — NVIDIA only).

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

use crate::cloud_detect;
use crate::core::nvml_reader;

/// Active GPU runtime kind. The string values match Python EXACTLY —
/// events serialize with these strings cross-SDK.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GpuRuntimeKind {
    Modal,
    Runpod,
    Replicate,
    LambdaLabs,
    Coreweave,
    AwsEc2Gpu,
    GcpGceBundled,
    GcpGceN1Attached,
    AzureVmGpu,
    AzureVmVgpu,
    None,
}

impl GpuRuntimeKind {
    /// String value (matches the Python `GpuRuntimeKind` enum values exactly).
    pub fn as_str(&self) -> &'static str {
        match self {
            GpuRuntimeKind::Modal => "modal",
            GpuRuntimeKind::Runpod => "runpod",
            GpuRuntimeKind::Replicate => "replicate",
            GpuRuntimeKind::LambdaLabs => "lambda_labs",
            GpuRuntimeKind::Coreweave => "coreweave",
            GpuRuntimeKind::AwsEc2Gpu => "aws_ec2_gpu",
            GpuRuntimeKind::GcpGceBundled => "gcp_gce_bundled",
            GpuRuntimeKind::GcpGceN1Attached => "gcp_gce_n1_attached",
            GpuRuntimeKind::AzureVmGpu => "azure_vm_gpu",
            GpuRuntimeKind::AzureVmVgpu => "azure_vm_vgpu",
            GpuRuntimeKind::None => "none",
        }
    }
}

// ── GPU instance-family regexes ────────────────────────────────────────────

fn aws_gpu_family_re() -> &'static Regex {
    static SLOT: OnceLock<Regex> = OnceLock::new();
    SLOT.get_or_init(|| {
        Regex::new(r"(?i)^(g4|g4dn|g5|g5g|g6|g6e|p3|p4d|p4de|p5|p5e|p5en)\.")
            .expect("aws GPU regex compiles")
    })
}

fn gcp_bundled_gpu_family_re() -> &'static Regex {
    static SLOT: OnceLock<Regex> = OnceLock::new();
    SLOT.get_or_init(|| Regex::new(r"(?i)^(a2|a3|a4|g2)-").expect("gcp bundled GPU regex compiles"))
}

fn gcp_n1_family_re() -> &'static Regex {
    static SLOT: OnceLock<Regex> = OnceLock::new();
    SLOT.get_or_init(|| Regex::new(r"(?i)^n1-").expect("gcp n1 regex compiles"))
}

fn azure_gpu_family_re() -> &'static Regex {
    static SLOT: OnceLock<Regex> = OnceLock::new();
    SLOT.get_or_init(|| Regex::new(r"(?i)^Standard_(ND|NC)").expect("azure ND/NC regex compiles"))
}

fn azure_vgpu_family_re() -> &'static Regex {
    static SLOT: OnceLock<Regex> = OnceLock::new();
    SLOT.get_or_init(|| {
        Regex::new(r"(?i)^Standard_NV\d+ads_A10_v5").expect("azure NVadsA10 regex compiles")
    })
}

fn is_match(re: &Regex, opt: Option<&str>) -> bool {
    opt.map(|s| re.is_match(s)).unwrap_or(false)
}

// ── Resolver ───────────────────────────────────────────────────────────────

/// Return the active GPU runtime, or `None` when there's no GPU.
///
/// The cascade short-circuits on the FIRST positive match. If NVML can't
/// initialize or reports 0 devices, returns `None` regardless of env-var
/// signals — a Modal task on a CPU-only Modal function emits no GPU events.
pub fn resolve_gpu_runtime() -> GpuRuntimeKind {
    // NVML must be available AND see ≥1 device for any GPU event emission.
    if !nvml_reader::nvml_available() {
        return GpuRuntimeKind::None;
    }
    let count = nvml_reader::get_device_count().unwrap_or(0);
    if count == 0 {
        return GpuRuntimeKind::None;
    }
    resolve_with_env_and_cloud(
        |k| std::env::var(k).ok(),
        cloud_detect::get_cloud_env(),
    )
}

/// Test-seam variant — takes the env var lookup + CloudEnv as parameters
/// so we can drive the cascade deterministically. The public
/// `resolve_gpu_runtime` adds the NVML gate.
pub(crate) fn resolve_with_env_and_cloud(
    env: impl Fn(&str) -> Option<String>,
    cloud: cloud_detect::CloudEnv,
) -> GpuRuntimeKind {
    // 1. Serverless GPU env vars
    if env("MODAL_TASK_ID").is_some() || env("MODAL_IMAGE_ID").is_some() {
        return GpuRuntimeKind::Modal;
    }
    if env("RUNPOD_POD_ID").is_some() || env("RUNPOD_POD_HOSTNAME").is_some() {
        return GpuRuntimeKind::Runpod;
    }
    if env("REPLICATE_MODEL").is_some() || env("REPLICATE_PREDICTION_ID").is_some() {
        return GpuRuntimeKind::Replicate;
    }

    let provider = cloud.provider.as_deref();
    let instance_type = cloud.instance_type.as_deref();

    if provider == Some("lambda_labs") {
        return GpuRuntimeKind::LambdaLabs;
    }
    if provider == Some("coreweave") {
        return GpuRuntimeKind::Coreweave;
    }

    if provider == Some("aws") && is_match(aws_gpu_family_re(), instance_type) {
        return GpuRuntimeKind::AwsEc2Gpu;
    }

    if provider == Some("gcp") {
        if is_match(gcp_bundled_gpu_family_re(), instance_type) {
            return GpuRuntimeKind::GcpGceBundled;
        }
        if is_match(gcp_n1_family_re(), instance_type) {
            // Decision #9 — N1 + attached accelerator detected via NVML only.
            return GpuRuntimeKind::GcpGceN1Attached;
        }
    }

    if provider == Some("azure") {
        // vGPU first — more specific regex than the broader ND/NC matcher.
        if is_match(azure_vgpu_family_re(), instance_type) {
            return GpuRuntimeKind::AzureVmVgpu;
        }
        if is_match(azure_gpu_family_re(), instance_type) {
            return GpuRuntimeKind::AzureVmGpu;
        }
    }

    GpuRuntimeKind::None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cloud_detect::CloudEnv;

    fn empty_env(_: &str) -> Option<String> {
        None
    }

    fn cloud(provider: Option<&str>, instance: Option<&str>) -> CloudEnv {
        CloudEnv {
            provider: provider.map(String::from),
            region: None,
            source: "test",
            instance_type: instance.map(String::from),
        }
    }

    #[test]
    fn modal_env_wins() {
        let env = |k: &str| match k {
            "MODAL_TASK_ID" => Some("abc".to_string()),
            _ => None,
        };
        assert_eq!(
            resolve_with_env_and_cloud(env, cloud(None, None)),
            GpuRuntimeKind::Modal
        );
    }

    #[test]
    fn runpod_env_wins() {
        let env = |k: &str| match k {
            "RUNPOD_POD_ID" => Some("abc".to_string()),
            _ => None,
        };
        assert_eq!(
            resolve_with_env_and_cloud(env, cloud(None, None)),
            GpuRuntimeKind::Runpod
        );
    }

    #[test]
    fn replicate_env_wins() {
        let env = |k: &str| match k {
            "REPLICATE_MODEL" => Some("m".to_string()),
            _ => None,
        };
        assert_eq!(
            resolve_with_env_and_cloud(env, cloud(None, None)),
            GpuRuntimeKind::Replicate
        );
    }

    #[test]
    fn aws_p5_is_gpu() {
        let env = empty_env;
        assert_eq!(
            resolve_with_env_and_cloud(env, cloud(Some("aws"), Some("p5.48xlarge"))),
            GpuRuntimeKind::AwsEc2Gpu
        );
    }

    #[test]
    fn aws_g6_is_gpu() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("aws"), Some("g6.xlarge"))),
            GpuRuntimeKind::AwsEc2Gpu
        );
    }

    #[test]
    fn aws_c7g_is_not_gpu() {
        // CPU-only — must NOT match.
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("aws"), Some("c7g.xlarge"))),
            GpuRuntimeKind::None
        );
    }

    #[test]
    fn gcp_a3_is_bundled() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("gcp"), Some("a3-highgpu-8g"))),
            GpuRuntimeKind::GcpGceBundled
        );
    }

    #[test]
    fn gcp_n1_is_n1_attached() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("gcp"), Some("n1-standard-8"))),
            GpuRuntimeKind::GcpGceN1Attached
        );
    }

    #[test]
    fn gcp_e2_is_not_gpu() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("gcp"), Some("e2-medium"))),
            GpuRuntimeKind::None
        );
    }

    #[test]
    fn azure_nd_is_gpu() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("azure"), Some("Standard_ND96isr_H100_v5"))),
            GpuRuntimeKind::AzureVmGpu
        );
    }

    #[test]
    fn azure_nc_is_gpu() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("azure"), Some("Standard_NC6s_v3"))),
            GpuRuntimeKind::AzureVmGpu
        );
    }

    #[test]
    fn azure_nvadsa10_v5_is_vgpu_not_gpu() {
        // Decision #10 — vGPU regex must beat the broader ND/NC matcher.
        assert_eq!(
            resolve_with_env_and_cloud(
                empty_env,
                cloud(Some("azure"), Some("Standard_NV6ads_A10_v5"))
            ),
            GpuRuntimeKind::AzureVmVgpu
        );
    }

    #[test]
    fn lambda_labs_provider() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("lambda_labs"), None)),
            GpuRuntimeKind::LambdaLabs
        );
    }

    #[test]
    fn coreweave_provider() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(Some("coreweave"), None)),
            GpuRuntimeKind::Coreweave
        );
    }

    #[test]
    fn nothing_matches_returns_none() {
        assert_eq!(
            resolve_with_env_and_cloud(empty_env, cloud(None, None)),
            GpuRuntimeKind::None
        );
    }

    #[test]
    fn enum_string_values_match_python() {
        // Cross-SDK contract — strings serialize identically.
        assert_eq!(GpuRuntimeKind::Modal.as_str(), "modal");
        assert_eq!(GpuRuntimeKind::Runpod.as_str(), "runpod");
        assert_eq!(GpuRuntimeKind::Replicate.as_str(), "replicate");
        assert_eq!(GpuRuntimeKind::LambdaLabs.as_str(), "lambda_labs");
        assert_eq!(GpuRuntimeKind::Coreweave.as_str(), "coreweave");
        assert_eq!(GpuRuntimeKind::AwsEc2Gpu.as_str(), "aws_ec2_gpu");
        assert_eq!(GpuRuntimeKind::GcpGceBundled.as_str(), "gcp_gce_bundled");
        assert_eq!(
            GpuRuntimeKind::GcpGceN1Attached.as_str(),
            "gcp_gce_n1_attached"
        );
        assert_eq!(GpuRuntimeKind::AzureVmGpu.as_str(), "azure_vm_gpu");
        assert_eq!(GpuRuntimeKind::AzureVmVgpu.as_str(), "azure_vm_vgpu");
        assert_eq!(GpuRuntimeKind::None.as_str(), "none");
    }
}
