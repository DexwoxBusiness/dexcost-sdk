use std::collections::HashMap;
use std::env;
use std::path::PathBuf;

use crate::error::DexcostError;

const DEFAULT_ENDPOINT: &str = "https://api.dexcost.io";

/// Returns true if the given URL is an acceptable DEXCOST_ENDPOINT.
///
/// Production traffic must be `https://`. As a documented exception
/// (matching standard browser security models for localhost),
/// `http://localhost[:port]/...` and `http://127.0.0.1[:port]/...`
/// are also accepted so that mock servers used in tests (wiremock,
/// httpbin, etc.) don't trigger the allow-list fallback.
/// Sprint 2 Theme D follow-on to A2 (commit 64bd3dd).
fn is_allowed_endpoint(url: &str) -> bool {
    if url.starts_with("https://") {
        return true;
    }
    if url.starts_with("http://localhost") || url.starts_with("http://127.0.0.1") {
        return true;
    }
    false
}

/// SDK configuration.
#[derive(Debug, Clone)]
pub struct Config {
    /// API key for the control layer. Must start with `dx_live_` or `dx_test_`.
    /// Falls back to `DEXCOST_API_KEY` environment variable.
    pub api_key: Option<String>,
    /// Maximum number of events per flush batch.
    pub batch_size: usize,
    /// Interval in seconds between automatic flushes.
    pub flush_interval_secs: u64,
    /// Field names to redact from event details before pushing.
    pub redact_fields: Vec<String>,
    /// When `true`, customer_id values in event details are SHA-256 hashed
    /// before being pushed to the control layer.
    pub hash_customer_id: bool,
    /// Deployment environment (e.g. "production", "development").
    /// Falls back to `DEXCOST_ENV` environment variable.
    pub environment: Option<String>,
    /// Names of SDKs to auto-instrument (e.g. `["openai", "anthropic"]`).
    /// Empty means no explicit auto-instrumentation request.
    pub auto_instrument: Vec<String>,
    /// When `true`, HTTP calls are tracked via the service catalog.
    /// Defaults to `true` to match the Python SDK.
    pub track_http: bool,
    /// Optional URL of a remote service catalog to merge at init time.
    pub service_catalog_url: Option<String>,
    /// Optional explicit path for the on-disk SQLite event buffer.
    /// When `None`, the default `~/.dexcost/buffer.db` location is used.
    pub buffer_path: Option<PathBuf>,
    /// Per-billing-model compute rate overrides keyed by rate name
    /// (e.g. `{"lambda_request_usd": "0.5"}`). Mirrors the Python
    /// `compute_billing_overrides` init knob.
    pub compute_billing_overrides: HashMap<String, String>,
    /// Opt-in K8s node-aware compute billing (Decision #11). When `false`
    /// (default), k8s pods bill at the per-vCPU-hour default rate.
    pub k8s_node_aware: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            api_key: None,
            batch_size: 100,
            flush_interval_secs: 5,
            redact_fields: Vec::new(),
            hash_customer_id: false,
            environment: None,
            auto_instrument: Vec::new(),
            track_http: true,
            service_catalog_url: None,
            buffer_path: None,
            compute_billing_overrides: HashMap::new(),
            k8s_node_aware: false,
        }
    }
}

impl Config {
    /// Creates a new Config with defaults.
    pub fn new() -> Self {
        Self::default()
    }

    /// Control Layer endpoint. Hardcoded default, overridable via
    /// DEXCOST_ENDPOINT env var. Sprint 1 Theme A / §2.1 (A2): only
    /// `https://` URLs are accepted. An attacker who controls the env
    /// (misconfigured CI runner, hostile container) could otherwise
    /// silently exfiltrate cost telemetry to an HTTP collector — we
    /// refuse and fall back to the production default.
    pub(crate) fn endpoint(&self) -> String {
        match env::var("DEXCOST_ENDPOINT") {
            Ok(v) if is_allowed_endpoint(&v) => v,
            Ok(v) => {
                eprintln!(
                    "dexcost: DEXCOST_ENDPOINT={:?} rejected — only https:// \
                     (or http://localhost / http://127.0.0.1 for tests) URLs \
                     are accepted. Falling back to {}.",
                    v, DEFAULT_ENDPOINT
                );
                DEFAULT_ENDPOINT.to_string()
            }
            Err(_) => DEFAULT_ENDPOINT.to_string(),
        }
    }

    /// Validates and resolves the configuration, reading env vars as fallback.
    pub fn validate(&mut self) -> Result<(), DexcostError> {
        // Resolve API key from env if not set
        if self.api_key.is_none() {
            if let Ok(key) = env::var("DEXCOST_API_KEY") {
                if !key.is_empty() {
                    self.api_key = Some(key);
                }
            }
        }

        // Validate API key format if present
        if let Some(ref key) = self.api_key {
            validate_api_key(key)?;
        }

        // Resolve environment from env if not set
        if self.environment.is_none() {
            if let Ok(env_val) = env::var("DEXCOST_ENV") {
                if !env_val.is_empty() {
                    self.environment = Some(env_val);
                }
            }
        }

        // Enable dev mode if environment is "development"
        if let Some(ref env_val) = self.environment {
            if env_val == "development" {
                crate::dev_console::enable_dev_mode();
            }
        }

        Ok(())
    }

    /// Returns the key type: "live", "test", or None.
    pub fn key_type(&self) -> Option<&str> {
        self.api_key.as_ref().and_then(|key| {
            if key.starts_with("dx_live_") {
                Some("live")
            } else if key.starts_with("dx_test_") {
                Some("test")
            } else {
                None
            }
        })
    }

    /// Returns true when using a test/sandbox API key.
    pub fn is_sandbox(&self) -> bool {
        self.key_type() == Some("test")
    }
}

/// Validates the API key format. Must start with `dx_live_` or `dx_test_`.
/// Empty string is allowed (no key).
pub fn validate_api_key(key: &str) -> Result<(), DexcostError> {
    if key.is_empty() {
        return Ok(());
    }
    if key.starts_with("dx_live_") || key.starts_with("dx_test_") {
        return Ok(());
    }
    let preview = if key.len() > 10 {
        format!("{}...", &key[..10])
    } else {
        key.to_string()
    };
    Err(DexcostError::InvalidApiKey(format!(
        "key must start with 'dx_live_' or 'dx_test_', got '{}'",
        preview
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_api_key_live() {
        assert!(validate_api_key("dx_live_abc123").is_ok());
    }

    #[test]
    fn test_validate_api_key_test() {
        assert!(validate_api_key("dx_test_abc123").is_ok());
    }

    #[test]
    fn test_validate_api_key_empty() {
        assert!(validate_api_key("").is_ok());
    }

    #[test]
    fn test_validate_api_key_invalid() {
        let result = validate_api_key("sk-invalid-key-format");
        assert!(result.is_err());
    }

    #[test]
    fn test_config_defaults() {
        let config = Config::default();
        assert_eq!(config.batch_size, 100);
        assert_eq!(config.flush_interval_secs, 5);
        assert!(config.api_key.is_none());
    }

    #[test]
    fn test_endpoint_default() {
        std::env::remove_var("DEXCOST_ENDPOINT");
        let config = Config::default();
        assert_eq!(config.endpoint(), "https://api.dexcost.io");
    }

    #[test]
    fn test_endpoint_env_override() {
        std::env::set_var("DEXCOST_ENDPOINT", "https://custom.api.dev");
        let config = Config::default();
        assert_eq!(config.endpoint(), "https://custom.api.dev");
        std::env::remove_var("DEXCOST_ENDPOINT");
    }

    /// A2 regression — Sprint 1 Theme A / §2.1. Non-https endpoint values
    /// are rejected (warn + fall back to production default) so a hostile
    /// env (misconfigured CI runner, compromised container) cannot
    /// exfiltrate telemetry to an HTTP collector.
    #[test]
    fn test_endpoint_rejects_http_falls_back_to_default() {
        std::env::set_var("DEXCOST_ENDPOINT", "http://attacker.example/");
        let config = Config::default();
        assert_eq!(config.endpoint(), "https://api.dexcost.io");
        std::env::remove_var("DEXCOST_ENDPOINT");
    }

    #[test]
    fn test_endpoint_rejects_arbitrary_scheme() {
        std::env::set_var("DEXCOST_ENDPOINT", "javascript:alert(1)");
        let config = Config::default();
        assert_eq!(config.endpoint(), "https://api.dexcost.io");
        std::env::remove_var("DEXCOST_ENDPOINT");
    }

    #[test]
    fn test_key_type() {
        let mut config = Config::default();
        assert!(config.key_type().is_none());

        config.api_key = Some("dx_live_abc".to_string());
        assert_eq!(config.key_type(), Some("live"));

        config.api_key = Some("dx_test_abc".to_string());
        assert_eq!(config.key_type(), Some("test"));
    }

    #[test]
    fn test_is_sandbox() {
        let mut config = Config::default();
        assert!(!config.is_sandbox());

        config.api_key = Some("dx_test_abc".to_string());
        assert!(config.is_sandbox());

        config.api_key = Some("dx_live_abc".to_string());
        assert!(!config.is_sandbox());
    }
}
