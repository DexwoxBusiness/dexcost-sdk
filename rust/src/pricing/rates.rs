use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// A single non-LLM service rate entry.
#[derive(Debug, Clone)]
pub struct RateEntry {
    pub service: String,
    pub per: String,
    pub cost_usd: Decimal,
}

/// On-disk JSON form of a rate entry (legacy format). Decimals are stored as
/// strings to preserve full precision through round-trips.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct RateEntryJson {
    service: String,
    per: String,
    cost_usd: String,
}

/// The per-service body of a YAML rate entry (`per` + `cost_usd`).
#[derive(Debug, Clone, Serialize, Deserialize)]
struct RateInfoYaml {
    #[serde(default = "default_per")]
    per: String,
    cost_usd: String,
}

fn default_per() -> String {
    "unit".to_string()
}

/// Top-level YAML document: `{ rates: { service -> {per, cost_usd} } }`.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct RatesFileYaml {
    #[serde(default)]
    rates: std::collections::BTreeMap<String, RateInfoYaml>,
}

/// Registry of non-LLM service cost rates with deterministic versioning.
pub struct RateRegistry {
    rates: HashMap<String, RateEntry>,
    version: Option<String>,
}

impl RateRegistry {
    /// Creates an empty RateRegistry.
    pub fn new() -> Self {
        Self {
            rates: HashMap::new(),
            version: None,
        }
    }

    /// Registers (or overwrites) a rate for a service. Invalidates the cached version.
    pub fn register(&mut self, service: &str, per: &str, cost_usd: Decimal) {
        self.rates.insert(
            service.to_string(),
            RateEntry {
                service: service.to_string(),
                per: per.to_string(),
                cost_usd,
            },
        );
        // Invalidate cached version
        self.version = None;
    }

    /// Returns the rate entry for a service, or None if not registered.
    pub fn get(&self, service: &str) -> Option<&RateEntry> {
        self.rates.get(service)
    }

    /// Returns a reference to all rates.
    pub fn rates(&self) -> &HashMap<String, RateEntry> {
        &self.rates
    }

    /// Number of registered rates.
    pub fn len(&self) -> usize {
        self.rates.len()
    }

    /// Whether the registry has any rates.
    pub fn is_empty(&self) -> bool {
        self.rates.is_empty()
    }

    /// Loads rates from a YAML config file and merges them into this registry.
    /// Existing rates with the same service key are overwritten.
    ///
    /// The expected format is a top-level `rates:` mapping (matching the
    /// Python SDK):
    ///
    /// ```yaml
    /// rates:
    ///   maps.googleapis.com:
    ///     per: request
    ///     cost_usd: "0.005"
    /// ```
    ///
    /// For backward compatibility, a legacy flat JSON array of
    /// `{service, per, cost_usd}` objects is also accepted.
    pub fn load_from_file(&mut self, path: &Path) -> Result<usize, String> {
        let contents = fs::read_to_string(path)
            .map_err(|e| format!("failed to read '{}': {}", path.display(), e))?;

        // Try YAML (`rates:` mapping) first.
        match serde_yaml_ng::from_str::<RatesFileYaml>(&contents) {
            Ok(doc) if !doc.rates.is_empty() => {
                let mut count = 0;
                for (service, info) in doc.rates {
                    let cost: Decimal = info.cost_usd.parse().map_err(|_| {
                        format!(
                            "invalid cost_usd '{}' for service '{}' in {}",
                            info.cost_usd,
                            service,
                            path.display()
                        )
                    })?;
                    self.register(&service, &info.per, cost);
                    count += 1;
                }
                return Ok(count);
            }
            _ => {}
        }

        // Fall back to the legacy flat JSON array form.
        let entries: Vec<RateEntryJson> = serde_json::from_str(&contents).map_err(|e| {
            format!(
                "invalid rates file '{}' (not a YAML 'rates:' mapping nor a legacy JSON array): {}",
                path.display(),
                e
            )
        })?;

        let mut count = 0;
        for entry in entries {
            let cost: Decimal = entry.cost_usd.parse().map_err(|_| {
                format!(
                    "invalid cost_usd '{}' for service '{}' in {}",
                    entry.cost_usd,
                    entry.service,
                    path.display()
                )
            })?;
            self.register(&entry.service, &entry.per, cost);
            count += 1;
        }
        Ok(count)
    }

    /// Writes the registry contents to a YAML file under a top-level `rates:`
    /// key. Entries are sorted by service name for deterministic output.
    pub fn save_to_file(&self, path: &Path) -> Result<usize, String> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)
                    .map_err(|e| format!("cannot create '{}': {}", parent.display(), e))?;
            }
        }

        // BTreeMap keeps services sorted by name for deterministic output.
        let mut rates: std::collections::BTreeMap<String, RateInfoYaml> =
            std::collections::BTreeMap::new();
        for entry in self.rates.values() {
            rates.insert(
                entry.service.clone(),
                RateInfoYaml {
                    per: entry.per.clone(),
                    cost_usd: entry.cost_usd.to_string(),
                },
            );
        }
        let count = rates.len();
        let doc = RatesFileYaml { rates };

        let yaml = serde_yaml_ng::to_string(&doc)
            .map_err(|e| format!("failed to serialise rates: {}", e))?;
        fs::write(path, yaml)
            .map_err(|e| format!("failed to write '{}': {}", path.display(), e))?;
        Ok(count)
    }

    /// Returns the default on-disk path for the registry: `~/.dexcost/rates.yaml`.
    /// Falls back to `./rates.yaml` if the home directory cannot be determined.
    pub fn default_path() -> PathBuf {
        if let Ok(p) = std::env::var("DEXCOST_RATES_PATH") {
            return PathBuf::from(p);
        }
        match dirs_next::home_dir() {
            Some(home) => home.join(".dexcost").join("rates.yaml"),
            None => PathBuf::from("rates.yaml"),
        }
    }

    /// Returns a 12-char SHA-256 prefix derived from the sorted rate entries.
    /// Format: "service:per:cost|service:per:cost|..." sorted by service name.
    /// Result is cached and invalidated on register.
    pub fn pricing_version(&mut self) -> String {
        if let Some(ref v) = self.version {
            return v.clone();
        }

        let version = self.pricing_version_snapshot();
        self.version = Some(version.clone());
        version
    }

    /// Returns the deterministic pricing version without mutating the cache.
    /// This is used by synchronous adapters that receive a shared registry
    /// reference but still need to attach authoritative cost evidence.
    pub fn pricing_version_snapshot(&self) -> String {
        if let Some(ref version) = self.version {
            return version.clone();
        }

        // Sort keys for deterministic ordering
        let mut keys: Vec<&String> = self.rates.keys().collect();
        keys.sort();

        let payload = keys
            .iter()
            .map(|k| {
                let e = &self.rates[*k];
                format!("{}:{}:{}", e.service, e.per, e.cost_usd)
            })
            .collect::<Vec<_>>()
            .join("|");

        let mut hasher = Sha256::new();
        hasher.update(payload.as_bytes());
        let hash = hasher.finalize();
        hex::encode(&hash[..6]) // first 12 hex chars
    }
}

impl Default for RateRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Decimal {
        s.parse().unwrap()
    }

    // 1. Register and retrieve a rate
    #[test]
    fn test_register_and_get() {
        let mut registry = RateRegistry::new();
        registry.register("stripe", "per_transaction", d("0.029"));

        let entry = registry.get("stripe").expect("rate should exist");
        assert_eq!(entry.service, "stripe");
        assert_eq!(entry.per, "per_transaction");
        assert_eq!(entry.cost_usd, d("0.029"));
    }

    // 2. Returns None for unregistered service
    #[test]
    fn test_get_unregistered_returns_none() {
        let registry = RateRegistry::new();
        assert!(registry.get("nonexistent").is_none());
    }

    // 3. Overwrites on re-register
    #[test]
    fn test_overwrite_on_reregister() {
        let mut registry = RateRegistry::new();
        registry.register("twilio", "per_sms", d("0.0075"));
        registry.register("twilio", "per_sms", d("0.0050"));

        let entry = registry.get("twilio").expect("rate should exist");
        assert_eq!(entry.cost_usd, d("0.0050"));
    }

    // 4. Pricing version is deterministic (same rates in different order = same hash)
    #[test]
    fn test_pricing_version_deterministic() {
        let mut r1 = RateRegistry::new();
        r1.register("stripe", "per_transaction", d("0.029"));
        r1.register("twilio", "per_sms", d("0.0075"));

        let mut r2 = RateRegistry::new();
        r2.register("twilio", "per_sms", d("0.0075"));
        r2.register("stripe", "per_transaction", d("0.029"));

        assert_eq!(r1.pricing_version(), r2.pricing_version());
    }

    #[test]
    fn test_readonly_pricing_version_matches_cached_version() {
        let mut registry = RateRegistry::new();
        registry.register("twilio", "per_sms", d("0.0075"));
        let snapshot = registry.pricing_version_snapshot();
        assert_eq!(snapshot, registry.pricing_version());
        assert_eq!(snapshot, registry.pricing_version_snapshot());
    }

    // 5. Version invalidates on new registration
    #[test]
    fn test_version_invalidates_on_register() {
        let mut registry = RateRegistry::new();
        registry.register("stripe", "per_transaction", d("0.029"));
        let v1 = registry.pricing_version();

        registry.register("sendgrid", "per_email", d("0.0001"));
        let v2 = registry.pricing_version();

        assert_ne!(v1, v2);
    }

    // 6. Returns all rates
    #[test]
    fn test_rates_returns_all() {
        let mut registry = RateRegistry::new();
        registry.register("stripe", "per_transaction", d("0.029"));
        registry.register("twilio", "per_sms", d("0.0075"));
        registry.register("sendgrid", "per_email", d("0.0001"));

        let all = registry.rates();
        assert_eq!(all.len(), 3);
        assert!(all.contains_key("stripe"));
        assert!(all.contains_key("twilio"));
        assert!(all.contains_key("sendgrid"));
    }

    // 7. Save round-trips through load (YAML format)
    #[test]
    fn test_save_then_load_round_trip() {
        let mut original = RateRegistry::new();
        original.register("stripe", "per_transaction", d("0.029"));
        original.register("twilio", "per_sms", d("0.0075"));

        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("rates.yaml");

        let written = original.save_to_file(&path).expect("save");
        assert_eq!(written, 2);
        assert!(path.exists());

        let mut loaded = RateRegistry::new();
        let n = loaded.load_from_file(&path).expect("load");
        assert_eq!(n, 2);

        assert_eq!(loaded.get("stripe").unwrap().cost_usd, d("0.029"));
        assert_eq!(loaded.get("twilio").unwrap().per, "per_sms");
    }

    // 7b. Saved file uses the `rates:` top-level YAML key (Python parity)
    #[test]
    fn test_save_writes_yaml_rates_key() {
        let mut registry = RateRegistry::new();
        registry.register("maps.googleapis.com", "request", d("0.005"));

        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("rates.yaml");
        registry.save_to_file(&path).expect("save");

        let contents = std::fs::read_to_string(&path).unwrap();
        assert!(contents.starts_with("rates:"), "contents:\n{}", contents);
        assert!(contents.contains("maps.googleapis.com"));
        assert!(contents.contains("per: request"));
        assert!(contents.contains("cost_usd:"));
    }

    // 7c. Loads a hand-written Python-style YAML rates file
    #[test]
    fn test_load_python_style_yaml() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("rates.yaml");
        std::fs::write(
            &path,
            "rates:\n  ocr-api.com:\n    per: page\n    cost_usd: \"0.01\"\n  search.com:\n    cost_usd: \"0.02\"\n",
        )
        .unwrap();

        let mut registry = RateRegistry::new();
        let n = registry.load_from_file(&path).expect("load");
        assert_eq!(n, 2);
        assert_eq!(registry.get("ocr-api.com").unwrap().per, "page");
        assert_eq!(registry.get("ocr-api.com").unwrap().cost_usd, d("0.01"));
        // Missing `per` defaults to "unit".
        assert_eq!(registry.get("search.com").unwrap().per, "unit");
    }

    // 7d. Legacy flat JSON array is still accepted for backward compatibility
    #[test]
    fn test_load_legacy_json_array() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("rates.json");
        std::fs::write(
            &path,
            r#"[{"service":"stripe","per":"per_transaction","cost_usd":"0.029"}]"#,
        )
        .unwrap();

        let mut registry = RateRegistry::new();
        let n = registry.load_from_file(&path).expect("load");
        assert_eq!(n, 1);
        assert_eq!(registry.get("stripe").unwrap().cost_usd, d("0.029"));
    }

    // 8. Save creates parent directory
    #[test]
    fn test_save_creates_parent_dir() {
        let mut registry = RateRegistry::new();
        registry.register("aws_s3", "per_request", d("0.0000004"));

        let dir = tempfile::tempdir().expect("tempdir");
        let nested = dir.path().join("subdir").join("rates.yaml");

        registry.save_to_file(&nested).expect("save");
        assert!(nested.exists());
    }

    // 9. Load surfaces invalid content
    #[test]
    fn test_load_invalid_content_errors() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("bad.yaml");
        std::fs::write(&path, "not yaml: [[[ {{{").unwrap();

        let mut registry = RateRegistry::new();
        let result = registry.load_from_file(&path);
        assert!(result.is_err());
    }

    // 10. Default path honours DEXCOST_RATES_PATH env var
    #[test]
    fn test_default_path_env_override() {
        // Use a unique env-var so concurrent tests don't fight.
        let key = "DEXCOST_RATES_PATH";
        let prev = std::env::var(key).ok();
        unsafe { std::env::set_var(key, "/custom/rates.json") };
        let path = RateRegistry::default_path();
        assert_eq!(path, std::path::PathBuf::from("/custom/rates.json"));
        match prev {
            Some(v) => unsafe { std::env::set_var(key, v) },
            None => unsafe { std::env::remove_var(key) },
        }
    }
}
