//! Active compute-runtime resolver.
//!
//! Cascade priority (capture spec §5.5):
//!
//!   1. Serverless env vars — Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
//!      Azure Functions, Vercel
//!   2. `KUBERNETES_SERVICE_HOST` → k8s_pod (wins over the underlying VM so a
//!      pod-on-EC2 is billed once as k8s_pod, not twice as k8s_pod + ec2)
//!   3. cloud_detect IaaS fallback — EC2 / GCE / Azure VM via the existing
//!      `CloudEnv.provider` resolved by `crate::cloud_detect`
//!   4. UNKNOWN
//!
//! Mirrors `python/src/dexcost/compute_runtime.py`. The `as_str()` values are
//! the discriminator strings persisted on `compute_cost` events; they MUST
//! match the Python enum string values exactly for cross-SDK event portability.

use crate::cloud_detect;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeKind {
    Lambda,
    Fargate,
    Ec2,
    CloudRun,
    CloudFunctions,
    Gce,
    AzureFunctions,
    AzureVm,
    Vercel,
    K8sPod,
    Unknown,
}

impl RuntimeKind {
    /// String discriminator persisted on compute_cost events. Cross-SDK
    /// invariant: these strings MUST match Python's `RuntimeKind` values.
    pub fn as_str(&self) -> &'static str {
        match self {
            RuntimeKind::Lambda => "lambda",
            RuntimeKind::Fargate => "fargate",
            RuntimeKind::Ec2 => "ec2",
            RuntimeKind::CloudRun => "cloud_run",
            RuntimeKind::CloudFunctions => "cloud_functions",
            RuntimeKind::Gce => "gce",
            RuntimeKind::AzureFunctions => "azure_functions",
            RuntimeKind::AzureVm => "azure_vm",
            RuntimeKind::Vercel => "vercel_fluid",
            RuntimeKind::K8sPod => "k8s_pod",
            RuntimeKind::Unknown => "unknown",
        }
    }
}

fn env_nonempty(name: &str) -> bool {
    matches!(std::env::var(name), Ok(v) if !v.is_empty())
}

/// Return the active compute runtime for the current process.
pub fn resolve_runtime() -> RuntimeKind {
    // 1. Serverless env vars
    if env_nonempty("AWS_LAMBDA_FUNCTION_NAME") {
        return RuntimeKind::Lambda;
    }
    if env_nonempty("ECS_CONTAINER_METADATA_URI_V4") || env_nonempty("ECS_CONTAINER_METADATA_URI") {
        return RuntimeKind::Fargate;
    }
    if env_nonempty("K_SERVICE") {
        if env_nonempty("FUNCTION_TARGET") {
            return RuntimeKind::CloudFunctions;
        }
        return RuntimeKind::CloudRun;
    }
    if env_nonempty("FUNCTIONS_WORKER_RUNTIME") {
        return RuntimeKind::AzureFunctions;
    }
    if env_nonempty("VERCEL") {
        return RuntimeKind::Vercel;
    }

    // 2. Kubernetes pod
    if env_nonempty("KUBERNETES_SERVICE_HOST") {
        return RuntimeKind::K8sPod;
    }

    // 3. cloud_detect IaaS fallback
    let env = cloud_detect::get_cloud_env();
    match env.provider.as_deref() {
        Some("aws") => RuntimeKind::Ec2,
        Some("gcp") => RuntimeKind::Gce,
        Some("azure") => RuntimeKind::AzureVm,
        _ => RuntimeKind::Unknown,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cloud_detect::{set_result_for_tests, CloudEnv};
    use std::sync::{LazyLock, Mutex as StdMutex};

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    const ENV_VARS: &[&str] = &[
        "AWS_LAMBDA_FUNCTION_NAME",
        "ECS_CONTAINER_METADATA_URI_V4",
        "ECS_CONTAINER_METADATA_URI",
        "K_SERVICE",
        "FUNCTION_TARGET",
        "FUNCTIONS_WORKER_RUNTIME",
        "VERCEL",
        "KUBERNETES_SERVICE_HOST",
    ];

    fn clear_env() {
        for v in ENV_VARS {
            // SAFETY: tests serialize via TEST_LOCK.
            unsafe { std::env::remove_var(v) };
        }
    }

    fn set_env(k: &str, v: &str) {
        // SAFETY: tests serialize via TEST_LOCK.
        unsafe { std::env::set_var(k, v) };
    }

    fn full_reset() {
        clear_env();
        set_result_for_tests(CloudEnv::none());
    }

    #[test]
    fn lambda_env_wins_over_all() {
        let _g = lock();
        full_reset();
        set_env("AWS_LAMBDA_FUNCTION_NAME", "my-fn");
        assert_eq!(resolve_runtime(), RuntimeKind::Lambda);
    }

    #[test]
    fn fargate_v4_env_resolves_fargate() {
        let _g = lock();
        full_reset();
        set_env("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/x");
        assert_eq!(resolve_runtime(), RuntimeKind::Fargate);
    }

    #[test]
    fn fargate_v3_env_resolves_fargate() {
        let _g = lock();
        full_reset();
        set_env("ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/x");
        assert_eq!(resolve_runtime(), RuntimeKind::Fargate);
    }

    #[test]
    fn k_service_alone_resolves_cloud_run() {
        let _g = lock();
        full_reset();
        set_env("K_SERVICE", "my-svc");
        assert_eq!(resolve_runtime(), RuntimeKind::CloudRun);
    }

    #[test]
    fn k_service_plus_function_target_resolves_cloud_functions() {
        let _g = lock();
        full_reset();
        set_env("K_SERVICE", "my-svc");
        set_env("FUNCTION_TARGET", "handler");
        assert_eq!(resolve_runtime(), RuntimeKind::CloudFunctions);
    }

    #[test]
    fn azure_functions_env_resolves_azure_functions() {
        let _g = lock();
        full_reset();
        set_env("FUNCTIONS_WORKER_RUNTIME", "python");
        assert_eq!(resolve_runtime(), RuntimeKind::AzureFunctions);
    }

    #[test]
    fn vercel_env_resolves_vercel() {
        let _g = lock();
        full_reset();
        set_env("VERCEL", "1");
        assert_eq!(resolve_runtime(), RuntimeKind::Vercel);
    }

    #[test]
    fn k8s_pod_wins_over_iaas_fallback() {
        let _g = lock();
        full_reset();
        set_env("KUBERNETES_SERVICE_HOST", "10.0.0.1");
        // Even with a CloudEnv saying aws, k8s_pod wins.
        set_result_for_tests(CloudEnv {
            provider: Some("aws".into()),
            region: Some("us-east-1".into()),
            source: "imds",
            instance_type: None,
        });
        assert_eq!(resolve_runtime(), RuntimeKind::K8sPod);
    }

    #[test]
    fn aws_cloud_env_resolves_ec2() {
        let _g = lock();
        full_reset();
        set_result_for_tests(CloudEnv {
            provider: Some("aws".into()),
            region: Some("us-east-1".into()),
            source: "imds",
            instance_type: Some("c7g.xlarge".into()),
        });
        assert_eq!(resolve_runtime(), RuntimeKind::Ec2);
    }

    #[test]
    fn gcp_cloud_env_resolves_gce() {
        let _g = lock();
        full_reset();
        set_result_for_tests(CloudEnv {
            provider: Some("gcp".into()),
            region: Some("us-central1".into()),
            source: "imds",
            instance_type: Some("n2-standard-2".into()),
        });
        assert_eq!(resolve_runtime(), RuntimeKind::Gce);
    }

    #[test]
    fn azure_cloud_env_resolves_azure_vm() {
        let _g = lock();
        full_reset();
        set_result_for_tests(CloudEnv {
            provider: Some("azure".into()),
            region: Some("eastus".into()),
            source: "imds",
            instance_type: Some("Standard_D2s_v3".into()),
        });
        assert_eq!(resolve_runtime(), RuntimeKind::AzureVm);
    }

    #[test]
    fn no_signals_resolves_unknown() {
        let _g = lock();
        full_reset();
        assert_eq!(resolve_runtime(), RuntimeKind::Unknown);
    }

    #[test]
    fn enum_strings_match_python_canonical() {
        // Cross-SDK invariant: these strings appear in persisted event details.
        assert_eq!(RuntimeKind::Lambda.as_str(), "lambda");
        assert_eq!(RuntimeKind::Fargate.as_str(), "fargate");
        assert_eq!(RuntimeKind::Ec2.as_str(), "ec2");
        assert_eq!(RuntimeKind::CloudRun.as_str(), "cloud_run");
        assert_eq!(RuntimeKind::CloudFunctions.as_str(), "cloud_functions");
        assert_eq!(RuntimeKind::Gce.as_str(), "gce");
        assert_eq!(RuntimeKind::AzureFunctions.as_str(), "azure_functions");
        assert_eq!(RuntimeKind::AzureVm.as_str(), "azure_vm");
        assert_eq!(RuntimeKind::Vercel.as_str(), "vercel_fluid");
        assert_eq!(RuntimeKind::K8sPod.as_str(), "k8s_pod");
        assert_eq!(RuntimeKind::Unknown.as_str(), "unknown");
    }
}
