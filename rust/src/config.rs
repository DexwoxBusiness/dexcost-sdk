use std::collections::HashMap;
use std::env;
use std::path::PathBuf;

use crate::error::DexcostError;

const DEFAULT_ENDPOINT: &str = "https://api.dexcost.io";

/// SDK configuration.
#[derive(Debug, Clone)]
pub struct Config {
    /// API key for the control layer. Must start with `dx_live_` or `dx_test_`.
    /// Falls back to `DEXCOST_API_KEY` environment variable.
    pub api_key: Option<String>,
    /// Control Layer endpoint. Explicit, in-code configuration only — the SDK
    /// never reads this from the process environment (closing the env-injection
    /// exfiltration vector). When `None` or empty, the hardcoded production
    /// `DEFAULT_ENDPOINT` is used. A set value must start with `http://` or
    /// `https://`, otherwise the default is used. `http://` is intentionally
    /// accepted here (e.g. `http://localhost` for e2e) because this field is
    /// developer-supplied and not attacker-controllable.
    pub endpoint: Option<String>,
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
    /// Optional URL of a conformant remote service catalog to install at init.
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

    // Sprint 3 Theme F / §4.1.3 (P4): network-event emission knobs,
    // parity with Python `init(network_event_*)`. The HTTP adapter
    // reads these to decide whether a captured call deserves an
    // emitted `network` event (in addition to the always-emitted
    // `external_cost`). Defaults match Python.
    /// Emit a `network` event when combined request+response bytes
    /// exceed this. Default 102_400 (100 KiB). Set 0 to disable.
    pub network_event_threshold_bytes: u64,
    /// Emit a `network` event on response status >= 400. Default true.
    pub network_event_on_error: bool,
    /// Emit a `network` event when call latency exceeds this many ms.
    /// Default 0 (latency trigger disabled).
    pub network_event_latency_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            api_key: None,
            endpoint: None,
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
            // P4 defaults — match Python init() values.
            network_event_threshold_bytes: 102_400,
            network_event_on_error: true,
            network_event_latency_ms: 0,
        }
    }
}

impl Config {
    /// Creates a new Config with defaults.
    pub fn new() -> Self {
        Self::default()
    }

    /// Control Layer endpoint. Resolved ONLY from the explicit, in-code
    /// `endpoint` field — never from the process environment. This removes the
    /// env-injection vector where an attacker controlling the process env (CI
    /// runner, hostile container) could set the endpoint to an HTTP collector
    /// and exfiltrate cost telemetry plus the Bearer API key.
    ///
    /// When the field is unset/empty, the hardcoded production `DEFAULT_ENDPOINT`
    /// is used. A set value must start with `http://` or `https://`; otherwise we
    /// warn and fall back to the default. `http://` is accepted because this
    /// field is developer-supplied and trusted (e.g. `http://localhost` for e2e).
    pub(crate) fn endpoint(&self) -> String {
        match self.endpoint.as_deref() {
            Some(v) if !v.is_empty() => {
                if v.starts_with("http://") || v.starts_with("https://") {
                    v.to_string()
                } else {
                    eprintln!(
                        "dexcost: Config.endpoint={:?} is not a valid URL — it must \
                         start with http:// or https://. Falling back to {}.",
                        v, DEFAULT_ENDPOINT
                    );
                    DEFAULT_ENDPOINT.to_string()
                }
            }
            _ => DEFAULT_ENDPOINT.to_string(),
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
        let config = Config::default();
        assert_eq!(config.endpoint(), "https://api.dexcost.io");
    }

    /// The endpoint comes from the explicit in-code `Config.endpoint` field.
    #[test]
    fn test_endpoint_explicit_option() {
        let config = Config {
            endpoint: Some("https://custom.api.dev".into()),
            ..Default::default()
        };
        assert_eq!(config.endpoint(), "https://custom.api.dev");
    }

    /// Threat closed: the SDK must NOT read the endpoint from the process env.
    /// Even with a hostile `DEXCOST_ENDPOINT` set, a default `Config` resolves
    /// to the hardcoded production endpoint — the env value is ignored entirely.
    #[test]
    fn test_endpoint_env_ignored() {
        std::env::set_var("DEXCOST_ENDPOINT", "http://evil.example");
        let config = Config::default();
        assert_eq!(config.endpoint(), DEFAULT_ENDPOINT);
        std::env::remove_var("DEXCOST_ENDPOINT");
    }

    /// Explicit `http://localhost` is developer-supplied and trusted (e.g. for
    /// e2e against a local server), so it is returned as-is.
    #[test]
    fn test_endpoint_explicit_http_localhost_accepted() {
        let config = Config {
            endpoint: Some("http://localhost:8080".into()),
            ..Default::default()
        };
        assert_eq!(config.endpoint(), "http://localhost:8080");
    }

    /// In-code validation: an explicit value with a non-http(s) scheme falls
    /// back to the production default.
    #[test]
    fn test_endpoint_explicit_bad_scheme_falls_back() {
        let config = Config {
            endpoint: Some("javascript:alert(1)".into()),
            ..Default::default()
        };
        assert_eq!(config.endpoint(), DEFAULT_ENDPOINT);
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
