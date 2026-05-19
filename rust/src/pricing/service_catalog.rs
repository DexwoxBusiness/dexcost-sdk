//! Service catalog for automatic non-LLM cost extraction.
//!
//! Loads `service_prices.json` (bundled at compile time via `include_str!`) and
//! provides:
//! - Domain -> service entry lookup
//! - Cost extraction from HTTP response headers/body
//! - User override registration
//! - Catalog version tracking (SHA-256 hash)

use std::collections::HashMap;

use rust_decimal::Decimal;
use sha2::{Digest, Sha256};

/// Bundled service price catalog, embedded at compile time.
const SERVICE_PRICES_JSON: &str = include_str!("../data/service_prices.json");

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

/// A single service entry from the catalog.
#[derive(Debug, Clone)]
pub struct ServiceEntry {
    pub key: String,
    pub display_name: String,
    pub domains: Vec<String>,
    pub category: String,
    pub pricing_model: String,
    pub cost_extraction: serde_json::Value,
    pub source: String,
    pub last_verified: String,
    pub endpoints: Option<Vec<String>>,
    /// Pricing fields that vary per service (e.g. `cost_per_credit_usd`).
    pub rate_fields: Option<HashMap<String, serde_json::Value>>,
    pub note: Option<String>,
}

/// Result of extracting cost from an HTTP response.
#[derive(Debug, Clone)]
pub struct CostExtractionResult {
    pub amount: Decimal,
    pub confidence: String,
    pub service_name: String,
    pub pricing_source: String,
}

/// A user override for a service entry.
#[derive(Debug, Clone)]
struct Override {
    cost_per_unit: Decimal,
    per: String,
}

// ---------------------------------------------------------------------------
// ServiceCatalog
// ---------------------------------------------------------------------------

/// Loads and queries the bundled service price catalog.
pub struct ServiceCatalog {
    entries: HashMap<String, ServiceEntry>,
    overrides: HashMap<String, Override>,
    raw_data: serde_json::Value,
}

impl ServiceCatalog {
    /// Creates a new ServiceCatalog from the bundled JSON data.
    pub fn new() -> Self {
        let raw_data: serde_json::Value = match serde_json::from_str(SERVICE_PRICES_JSON) {
            Ok(data) => data,
            Err(e) => {
                eprintln!(
                    "[dexcost] WARNING: failed to parse bundled service_prices.json: {}",
                    e
                );
                serde_json::Value::Object(Default::default())
            }
        };

        let entries = Self::parse_entries(&raw_data);

        Self {
            entries,
            overrides: HashMap::new(),
            raw_data,
        }
    }

    /// Creates a ServiceCatalog from a raw JSON string (for testing).
    pub fn from_json(json_str: &str) -> Self {
        let raw_data: serde_json::Value = match serde_json::from_str(json_str) {
            Ok(data) => data,
            Err(e) => {
                eprintln!(
                    "[dexcost] WARNING: failed to parse service catalog JSON: {}",
                    e
                );
                serde_json::Value::Object(Default::default())
            }
        };

        let entries = Self::parse_entries(&raw_data);

        Self {
            entries,
            overrides: HashMap::new(),
            raw_data,
        }
    }

    /// Parse all entries from the raw JSON data.
    fn parse_entries(data: &serde_json::Value) -> HashMap<String, ServiceEntry> {
        let mut entries = HashMap::new();
        if let serde_json::Value::Object(map) = data {
            for (key, entry_data) in map {
                if key == "_meta" {
                    continue;
                }
                if let Some(entry) = Self::parse_entry(key, entry_data) {
                    entries.insert(key.clone(), entry);
                }
            }
        }
        entries
    }

    /// Parse a single JSON entry into a ServiceEntry.
    fn parse_entry(key: &str, data: &serde_json::Value) -> Option<ServiceEntry> {
        let obj = data.as_object()?;

        let display_name = obj.get("display_name")?.as_str()?.to_string();
        let domains: Vec<String> = obj
            .get("domains")?
            .as_array()?
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
        let category = obj.get("category")?.as_str()?.to_string();
        let pricing_model = obj.get("pricing_model")?.as_str()?.to_string();
        let cost_extraction = obj.get("cost_extraction")?.clone();
        let source = obj.get("source")?.as_str()?.to_string();
        let last_verified = obj.get("last_verified")?.as_str()?.to_string();

        let endpoints = obj.get("endpoints").and_then(|v| {
            v.as_array().map(|arr| {
                arr.iter()
                    .filter_map(|e| e.as_str().map(String::from))
                    .collect()
            })
        });

        let note = obj.get("note").and_then(|v| v.as_str().map(String::from));

        // Collect rate fields (everything not in the standard set)
        let standard_keys: &[&str] = &[
            "display_name",
            "domains",
            "category",
            "pricing_model",
            "cost_extraction",
            "source",
            "last_verified",
            "endpoints",
            "note",
        ];
        let mut rate_fields = HashMap::new();
        for (k, v) in obj {
            if !standard_keys.contains(&k.as_str()) {
                rate_fields.insert(k.clone(), v.clone());
            }
        }

        Some(ServiceEntry {
            key: key.to_string(),
            display_name,
            domains,
            category,
            pricing_model,
            cost_extraction,
            source,
            last_verified,
            endpoints,
            rate_fields: if rate_fields.is_empty() {
                None
            } else {
                Some(rate_fields)
            },
            note,
        })
    }

    /// Match a URL against the catalog by domain and endpoint.
    ///
    /// Wildcard domains like `*.pinecone.io` are supported.
    /// When multiple entries share the same domain (e.g. Google Maps),
    /// endpoint matching is used to disambiguate.
    pub fn lookup(&self, url: &str) -> Option<&ServiceEntry> {
        let (hostname, path) = parse_url(url);

        // Collect all entries whose domains match
        let mut candidates: Vec<&ServiceEntry> = Vec::new();
        for entry in self.entries.values() {
            if domain_matches(&hostname, &entry.domains) {
                candidates.push(entry);
            }
        }

        if candidates.is_empty() {
            return None;
        }

        // If only one candidate, return it
        if candidates.len() == 1 {
            return Some(candidates[0]);
        }

        // Multiple candidates: filter by endpoint match
        for entry in &candidates {
            if let Some(ref endpoints) = entry.endpoints {
                for ep in endpoints {
                    if path.starts_with(ep.as_str()) {
                        return Some(entry);
                    }
                }
            }
        }

        // Fallback: return first candidate without endpoints requirement
        for entry in &candidates {
            if entry.endpoints.is_none() {
                return Some(entry);
            }
        }

        // Last resort: first candidate
        Some(candidates[0])
    }

    /// Apply extraction rules to get cost from an HTTP response.
    ///
    /// Returns `None` if cost cannot be extracted.
    pub fn extract_cost(
        &self,
        entry: &ServiceEntry,
        response_headers: &HashMap<String, String>,
        response_body: Option<&serde_json::Value>,
    ) -> Option<CostExtractionResult> {
        // Check user override first
        if let Some(ov) = self.overrides.get(&entry.key) {
            return Some(CostExtractionResult {
                amount: ov.cost_per_unit,
                confidence: "exact".to_string(),
                service_name: entry.display_name.clone(),
                pricing_source: "user_override".to_string(),
            });
        }

        let extraction = &entry.cost_extraction;
        let ext_type = extraction
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("fixed");

        match ext_type {
            "response_body" => self.extract_from_body(entry, extraction, response_body),
            "response_header" => self.extract_from_header(entry, extraction, response_headers),
            "endpoint_match" => self.extract_endpoint_match(entry),
            "fixed" => self.extract_fixed(entry),
            _ => None,
        }
    }

    /// Extract cost from a response body field.
    fn extract_from_body(
        &self,
        entry: &ServiceEntry,
        extraction: &serde_json::Value,
        response_body: Option<&serde_json::Value>,
    ) -> Option<CostExtractionResult> {
        match response_body {
            Some(body) => {
                let path = extraction
                    .get("path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                let value = resolve_dotted_path(body, path);

                match value {
                    Some(val) => {
                        let raw_value = decimal_from_json_value(val)?;
                        let transform = extraction.get("transform").and_then(|v| v.as_str());

                        let (amount, confidence) = if let Some(t) = transform {
                            (
                                apply_transform(t, raw_value, get_rate(entry)),
                                "computed".to_string(),
                            )
                        } else {
                            let rate = get_rate(entry);
                            let computed = match rate {
                                Some(r) => raw_value * r,
                                None => raw_value,
                            };
                            (computed, "computed".to_string())
                        };

                        Some(CostExtractionResult {
                            amount,
                            confidence,
                            service_name: entry.display_name.clone(),
                            pricing_source: "service_catalog".to_string(),
                        })
                    }
                    None => {
                        // Try fallback
                        self.try_fallback(entry, extraction)
                    }
                }
            }
            None => {
                // Use fallback credits if available
                self.try_fallback(entry, extraction)
            }
        }
    }

    /// Try fallback credits from the extraction config.
    fn try_fallback(
        &self,
        entry: &ServiceEntry,
        extraction: &serde_json::Value,
    ) -> Option<CostExtractionResult> {
        let fallback = extraction.get("fallback_credits")?;
        let fallback_val = decimal_from_json_value(fallback)?;
        let rate = get_rate(entry)?;
        let amount = fallback_val * rate;

        Some(CostExtractionResult {
            amount,
            confidence: "estimated".to_string(),
            service_name: entry.display_name.clone(),
            pricing_source: "service_catalog".to_string(),
        })
    }

    /// Extract cost from a response header.
    fn extract_from_header(
        &self,
        entry: &ServiceEntry,
        extraction: &serde_json::Value,
        response_headers: &HashMap<String, String>,
    ) -> Option<CostExtractionResult> {
        let header = extraction.get("header").and_then(|v| v.as_str())?;

        // Case-insensitive header lookup
        let header_lower = header.to_lowercase();
        let header_value = response_headers
            .iter()
            .find(|(k, _)| k.to_lowercase() == header_lower)
            .map(|(_, v)| v.as_str())?;

        let raw_value: Decimal = header_value.parse().ok()?;
        let rate = get_rate(entry);
        let amount = match rate {
            Some(r) => raw_value * r,
            None => raw_value,
        };

        Some(CostExtractionResult {
            amount,
            confidence: "computed".to_string(),
            service_name: entry.display_name.clone(),
            pricing_source: "service_catalog".to_string(),
        })
    }

    /// Fixed cost per request from endpoint match.
    fn extract_endpoint_match(&self, entry: &ServiceEntry) -> Option<CostExtractionResult> {
        let cost = get_fixed_cost(entry)?;
        Some(CostExtractionResult {
            amount: cost,
            confidence: "exact".to_string(),
            service_name: entry.display_name.clone(),
            pricing_source: "service_catalog".to_string(),
        })
    }

    /// Fixed cost per request.
    fn extract_fixed(&self, entry: &ServiceEntry) -> Option<CostExtractionResult> {
        let cost = get_fixed_cost(entry)?;
        Some(CostExtractionResult {
            amount: cost,
            confidence: "exact".to_string(),
            service_name: entry.display_name.clone(),
            pricing_source: "service_catalog".to_string(),
        })
    }

    /// Register a user override for a service entry.
    ///
    /// Takes precedence over catalog rates during extraction.
    pub fn register_override(&mut self, service_key: &str, cost_per_unit: Decimal, per: &str) {
        self.overrides.insert(
            service_key.to_string(),
            Override {
                cost_per_unit,
                per: per.to_string(),
            },
        );
    }

    /// Return a hash of the loaded data for pricing_version tracking.
    ///
    /// Returns the first 16 hex chars of the SHA-256 digest of the combined
    /// raw data and overrides (matching the Python SDK behavior).
    pub fn catalog_version(&self) -> String {
        let content = serde_json::to_string(&self.raw_data).unwrap_or_default();

        // Build sorted override representation
        let mut override_keys: Vec<&String> = self.overrides.keys().collect();
        override_keys.sort();

        let mut override_map = serde_json::Map::new();
        for key in override_keys {
            if let Some(ov) = self.overrides.get(key) {
                let mut entry = serde_json::Map::new();
                entry.insert(
                    "cost_per_unit".to_string(),
                    serde_json::Value::String(ov.cost_per_unit.to_string()),
                );
                entry.insert("per".to_string(), serde_json::Value::String(ov.per.clone()));
                override_map.insert(key.clone(), serde_json::Value::Object(entry));
            }
        }

        let override_content =
            serde_json::to_string(&serde_json::Value::Object(override_map)).unwrap_or_default();

        let combined = format!("{}{}", content, override_content);
        let digest = Sha256::digest(combined.as_bytes());
        hex::encode(&digest[..8]) // first 16 hex chars
    }

    /// Fetches a remote catalog JSON over HTTP and merges its entries into
    /// this catalog. New entries are added; existing entries are updated.
    ///
    /// Mirrors Python `service_catalog.py` `refresh_from_url`
    /// (`service_catalog.py:386-404`). The `_meta` key is skipped.
    pub async fn refresh_from_url(
        &mut self,
        url: &str,
    ) -> Result<usize, Box<dyn std::error::Error + Send + Sync>> {
        let resp = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()?
            .get(url)
            .send()
            .await?;

        if !resp.status().is_success() {
            return Err(format!("catalog refresh failed: HTTP {}", resp.status()).into());
        }

        let remote: serde_json::Value = resp.json().await?;
        let obj = remote
            .as_object()
            .ok_or("remote catalog must be a JSON object")?;

        let mut merged = 0usize;
        for (key, entry_data) in obj {
            if key == "_meta" {
                continue;
            }
            // Update the raw_data so catalog_version reflects the merge.
            if let serde_json::Value::Object(ref mut raw_map) = self.raw_data {
                raw_map.insert(key.clone(), entry_data.clone());
            }
            if let Some(entry) = Self::parse_entry(key, entry_data) {
                self.entries.insert(key.clone(), entry);
                merged += 1;
            }
        }
        Ok(merged)
    }

    /// Look up a service entry by its catalog key (e.g. `"tavily_search"`).
    pub fn get_by_key(&self, key: &str) -> Option<&ServiceEntry> {
        self.entries.get(key)
    }

    /// Return a reference to all loaded entries.
    pub fn entries(&self) -> &HashMap<String, ServiceEntry> {
        &self.entries
    }
}

impl Default for ServiceCatalog {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/// Parse a URL into (hostname, path). Returns empty strings on failure.
fn parse_url(url: &str) -> (String, String) {
    // Simple URL parsing -- extract host and path without pulling in the `url` crate.
    let without_scheme = if let Some(idx) = url.find("://") {
        &url[idx + 3..]
    } else {
        url
    };

    let (host_part, path) = match without_scheme.find('/') {
        Some(idx) => (&without_scheme[..idx], &without_scheme[idx..]),
        None => (without_scheme, "/"),
    };

    // Strip port if present
    let hostname = match host_part.find(':') {
        Some(idx) => &host_part[..idx],
        None => host_part,
    };

    (hostname.to_lowercase(), path.to_string())
}

/// Check if hostname matches any of the domain patterns.
fn domain_matches(hostname: &str, patterns: &[String]) -> bool {
    for pattern in patterns {
        if let Some(bare) = pattern.strip_prefix("*.") {
            let suffix = &pattern[1..]; // ".pinecone.io"
            if hostname.ends_with(suffix) || hostname == bare {
                return true;
            }
        } else if hostname == pattern {
            return true;
        }
    }
    false
}

/// Resolve a dotted path like "data.stats.computeUnits" in a JSON value.
fn resolve_dotted_path<'a>(
    data: &'a serde_json::Value,
    path: &str,
) -> Option<&'a serde_json::Value> {
    let parts: Vec<&str> = path.split('.').collect();
    let mut current = data;
    for part in parts {
        if part.is_empty() {
            continue;
        }
        current = current.get(part)?;
    }
    Some(current)
}

/// Try to parse a Decimal from a JSON value (number or string).
fn decimal_from_json_value(v: &serde_json::Value) -> Option<Decimal> {
    match v {
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Some(Decimal::from(i))
            } else if let Some(f) = n.as_f64() {
                Decimal::try_from(f).ok()
            } else {
                None
            }
        }
        serde_json::Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

/// Get the per-unit rate from the entry's rate fields.
fn get_rate(entry: &ServiceEntry) -> Option<Decimal> {
    let rate_fields = entry.rate_fields.as_ref()?;
    for (k, v) in rate_fields {
        if k.starts_with("cost_per_") && k.ends_with("_usd") {
            return decimal_from_json_value(v);
        }
    }
    None
}

/// Get the fixed cost per request from rate fields.
fn get_fixed_cost(entry: &ServiceEntry) -> Option<Decimal> {
    let rate_fields = entry.rate_fields.as_ref()?;
    for (k, v) in rate_fields {
        if k.starts_with("cost_per_") && k.ends_with("_usd") {
            return decimal_from_json_value(v);
        }
    }
    None
}

/// Apply a named transform to a raw value.
fn apply_transform(transform: &str, raw_value: Decimal, rate: Option<Decimal>) -> Decimal {
    match transform {
        "ms_to_seconds" => {
            let thousand = Decimal::new(1000, 0);
            let seconds = raw_value / thousand;
            match rate {
                Some(r) => seconds * r,
                None => Decimal::ZERO,
            }
        }
        "ms_to_minutes" => {
            let sixty_thousand = Decimal::new(60000, 0);
            let minutes = raw_value / sixty_thousand;
            match rate {
                Some(r) => minutes * r,
                None => Decimal::ZERO,
            }
        }
        "stripe_fee" => {
            // amount is in cents
            let hundred = Decimal::new(100, 0);
            let amount_dollars = raw_value / hundred;
            let pct = Decimal::new(29, 3); // 0.029
            let fixed = Decimal::new(30, 2); // 0.30
            amount_dollars * pct + fixed
        }
        _ => raw_value,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_catalog_loads_entries() {
        let catalog = ServiceCatalog::new();
        assert!(
            !catalog.entries().is_empty(),
            "catalog should have entries from bundled JSON"
        );
    }

    #[test]
    fn test_lookup_exact_domain() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://api.tavily.com/search");
        assert!(entry.is_some());
        assert_eq!(entry.unwrap().key, "tavily_search");
    }

    #[test]
    fn test_lookup_wildcard_domain() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://my-index.svc.us-east1-gcp.pinecone.io/query");
        assert!(entry.is_some());
        assert_eq!(entry.unwrap().key, "pinecone_query");
    }

    #[test]
    fn test_lookup_unknown_domain() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://unknown-service.example.com/api");
        assert!(entry.is_none());
    }

    #[test]
    fn test_lookup_endpoint_disambiguation() {
        let catalog = ServiceCatalog::new();
        let geo = catalog.lookup("https://maps.googleapis.com/maps/api/geocode/json");
        assert!(geo.is_some());
        assert_eq!(geo.unwrap().key, "google_maps_geocode");

        let places = catalog.lookup("https://maps.googleapis.com/maps/api/place/nearbysearch");
        assert!(places.is_some());
        assert_eq!(places.unwrap().key, "google_maps_places");
    }

    #[test]
    fn test_extract_fixed_cost() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://api.exa.ai/search").unwrap();

        let result = catalog.extract_cost(entry, &HashMap::new(), None);
        assert!(result.is_some());
        let r = result.unwrap();
        assert_eq!(r.confidence, "exact");
        assert_eq!(r.pricing_source, "service_catalog");
    }

    #[test]
    fn test_extract_from_header() {
        let catalog = ServiceCatalog::new();
        let entry = catalog
            .lookup("https://app.scrapingbee.com/api/v1")
            .unwrap();

        let mut headers = HashMap::new();
        headers.insert("Spb-cost".to_string(), "5".to_string());

        let result = catalog.extract_cost(entry, &headers, None);
        assert!(result.is_some());
        let r = result.unwrap();
        assert_eq!(r.confidence, "computed");
        assert!(r.amount > Decimal::ZERO);
    }

    #[test]
    fn test_extract_from_body() {
        let catalog = ServiceCatalog::new();
        let entry = catalog
            .lookup("https://my-index.svc.us-east1-gcp.pinecone.io/query")
            .unwrap();

        let body = serde_json::json!({
            "usage": {
                "readUnits": 10
            }
        });

        let result = catalog.extract_cost(entry, &HashMap::new(), Some(&body));
        assert!(result.is_some());
        let r = result.unwrap();
        assert_eq!(r.confidence, "computed");
        assert!(r.amount > Decimal::ZERO);
    }

    #[test]
    fn test_extract_with_transform_stripe_fee() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://api.stripe.com/v1/charges").unwrap();

        let body = serde_json::json!({
            "amount": 2000 // $20.00 in cents
        });

        let result = catalog.extract_cost(entry, &HashMap::new(), Some(&body));
        assert!(result.is_some());
        let r = result.unwrap();
        // $20 * 2.9% + $0.30 = $0.58 + $0.30 = $0.88
        assert_eq!(r.confidence, "computed");
        assert!(r.amount > Decimal::ZERO);
    }

    #[test]
    fn test_extract_fallback_credits() {
        let catalog = ServiceCatalog::new();
        let entry = catalog.lookup("https://api.tavily.com/search").unwrap();

        // No body -> should use fallback_credits
        let result = catalog.extract_cost(entry, &HashMap::new(), None);
        assert!(result.is_some());
        let r = result.unwrap();
        assert_eq!(r.confidence, "estimated");
    }

    #[test]
    fn test_register_override() {
        let mut catalog = ServiceCatalog::new();
        let override_cost = Decimal::new(50, 2); // 0.50
        catalog.register_override("tavily_search", override_cost, "request");

        let entry = catalog.lookup("https://api.tavily.com/search").unwrap();
        let result = catalog.extract_cost(entry, &HashMap::new(), None);
        assert!(result.is_some());
        let r = result.unwrap();
        assert_eq!(r.amount, override_cost);
        assert_eq!(r.confidence, "exact");
        assert_eq!(r.pricing_source, "user_override");
    }

    #[test]
    fn test_catalog_version_deterministic() {
        let c1 = ServiceCatalog::new();
        let c2 = ServiceCatalog::new();
        assert_eq!(c1.catalog_version(), c2.catalog_version());
    }

    #[test]
    fn test_catalog_version_changes_with_override() {
        let c1 = ServiceCatalog::new();
        let v1 = c1.catalog_version();

        let mut c2 = ServiceCatalog::new();
        c2.register_override("tavily_search", Decimal::new(1, 0), "request");
        let v2 = c2.catalog_version();

        assert_ne!(v1, v2);
    }

    #[test]
    fn test_domain_matches_exact() {
        assert!(domain_matches(
            "api.tavily.com",
            &["api.tavily.com".to_string()]
        ));
    }

    #[test]
    fn test_domain_matches_wildcard() {
        assert!(domain_matches(
            "my-index.svc.pinecone.io",
            &["*.pinecone.io".to_string()]
        ));
    }

    #[test]
    fn test_domain_matches_wildcard_bare() {
        assert!(domain_matches(
            "pinecone.io",
            &["*.pinecone.io".to_string()]
        ));
    }

    #[test]
    fn test_domain_no_match() {
        assert!(!domain_matches(
            "other.example.com",
            &["api.tavily.com".to_string()]
        ));
    }

    #[test]
    fn test_parse_url_basic() {
        let (host, path) = parse_url("https://api.tavily.com/search");
        assert_eq!(host, "api.tavily.com");
        assert_eq!(path, "/search");
    }

    #[test]
    fn test_parse_url_with_port() {
        let (host, path) = parse_url("https://api.example.com:8080/v1/data");
        assert_eq!(host, "api.example.com");
        assert_eq!(path, "/v1/data");
    }

    #[test]
    fn test_resolve_dotted_path() {
        let data = serde_json::json!({
            "data": {
                "stats": {
                    "computeUnits": 42
                }
            }
        });
        let result = resolve_dotted_path(&data, "data.stats.computeUnits");
        assert_eq!(result.unwrap().as_i64(), Some(42));
    }

    #[test]
    fn test_resolve_dotted_path_missing() {
        let data = serde_json::json!({"a": 1});
        let result = resolve_dotted_path(&data, "b.c");
        assert!(result.is_none());
    }

    #[test]
    fn test_apply_transform_ms_to_seconds() {
        let rate = Some(Decimal::new(14, 6)); // 0.000014
        let result = apply_transform("ms_to_seconds", Decimal::new(5000, 0), rate);
        // 5000ms = 5s, 5 * 0.000014 = 0.000070
        assert_eq!(result, Decimal::new(70, 6));
    }

    #[test]
    fn test_apply_transform_stripe_fee() {
        let result = apply_transform("stripe_fee", Decimal::new(2000, 0), None);
        // $20 * 2.9% + $0.30 = $0.58 + $0.30 = $0.88
        let expected = Decimal::new(88, 2);
        assert_eq!(result, expected);
    }

    #[test]
    fn test_apply_transform_unknown() {
        let result = apply_transform("unknown_transform", Decimal::new(42, 0), None);
        assert_eq!(result, Decimal::new(42, 0));
    }

    // Gap 7: refresh_from_url fetches and merges a remote catalog over HTTP.
    #[tokio::test]
    async fn test_refresh_from_url_merges_entries() {
        use wiremock::matchers::method;
        use wiremock::{Mock, MockServer, ResponseTemplate};

        let server = MockServer::start().await;
        let remote = serde_json::json!({
            "_meta": {"version": "ignored"},
            "custom_search_api": {
                "display_name": "Custom Search",
                "domains": ["api.customsearch.example"],
                "category": "search",
                "pricing_model": "per_request",
                "cost_extraction": {"type": "fixed"},
                "source": "test",
                "last_verified": "2026-01-01",
                "cost_per_request_usd": 0.01
            }
        });
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(&remote))
            .mount(&server)
            .await;

        let mut catalog = ServiceCatalog::new();
        assert!(catalog.get_by_key("custom_search_api").is_none());

        let merged = catalog
            .refresh_from_url(&server.uri())
            .await
            .expect("refresh should succeed");
        assert_eq!(merged, 1, "_meta is skipped, one real entry merged");

        let entry = catalog
            .get_by_key("custom_search_api")
            .expect("merged entry must be queryable");
        assert_eq!(entry.display_name, "Custom Search");
        // The merged URL should now resolve via lookup().
        assert!(catalog
            .lookup("https://api.customsearch.example/search")
            .is_some());
    }

    #[tokio::test]
    async fn test_refresh_from_url_unreachable_errors() {
        let mut catalog = ServiceCatalog::new();
        let result = catalog.refresh_from_url("http://127.0.0.1:1").await;
        assert!(result.is_err());
    }
}
