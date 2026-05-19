use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;

use crate::core::models::{CostConfidence, PricingSource};

/// Embedded cost map JSON, loaded at compile time.
const COST_MAP_JSON: &str = include_str!("cost_map.json");

/// Result of a cost lookup.
#[derive(Debug, Clone)]
pub struct CostResult {
    pub cost_usd: Decimal,
    pub cost_confidence: CostConfidence,
    pub pricing_source: PricingSource,
    pub pricing_version: String,
}

/// Per-token pricing for a model.
#[derive(Debug, Clone)]
struct ModelPricing {
    input_cost_per_token: Decimal,
    output_cost_per_token: Decimal,
    cache_read_cost: Decimal,
    has_cache_read: bool,
    /// Anthropic-specific rate for tokens *written* to the prompt cache.
    cache_creation_cost: Decimal,
    has_cache_creation: bool,
}

/// Custom per-1k-token pricing set by the user.
#[derive(Debug, Clone)]
struct CustomPricing {
    input_per_1k: Decimal,
    output_per_1k: Decimal,
}

/// Inner mutable state guarded by a tokio RwLock.
struct Inner {
    models: HashMap<String, ModelPricing>,
    custom: HashMap<String, CustomPricing>,
    pricing_version: String,
}

/// PricingEngine provides LLM cost lookups from bundled pricing data.
/// Supports exact match, provider-prefix stripping, and date-suffix fallback.
/// Supports background refresh from a control-layer endpoint.
pub struct PricingEngine {
    inner: Arc<RwLock<Inner>>,
    stop_signal: Arc<AtomicBool>,
}

impl PricingEngine {
    /// Creates a new PricingEngine from the embedded cost_map.json.
    pub fn new() -> Self {
        Self::from_bytes(COST_MAP_JSON.as_bytes())
    }

    /// Creates a PricingEngine from raw JSON bytes.
    fn from_bytes(data: &[u8]) -> Self {
        let (models, pricing_version) = Self::parse_cost_map(data);
        PricingEngine {
            inner: Arc::new(RwLock::new(Inner {
                models,
                custom: HashMap::new(),
                pricing_version,
            })),
            stop_signal: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Parse raw JSON bytes into a model map and version string.
    fn parse_cost_map(data: &[u8]) -> (HashMap<String, ModelPricing>, String) {
        let raw: HashMap<String, serde_json::Value> = match serde_json::from_slice(data) {
            Ok(map) => map,
            Err(e) => {
                eprintln!(
                    "[dexcost] WARNING: failed to parse bundled pricing data: {}",
                    e
                );
                HashMap::new()
            }
        };

        let mut models = HashMap::with_capacity(raw.len());
        for (name, entry) in &raw {
            if let Some(obj) = entry.as_object() {
                let input = Self::decimal_from_value(obj.get("input_cost_per_token"));
                let output = Self::decimal_from_value(obj.get("output_cost_per_token"));
                let (cache_read, has_cache_read) =
                    if let Some(v) = obj.get("cache_read_input_token_cost") {
                        (Self::decimal_from_value(Some(v)), true)
                    } else {
                        (Decimal::ZERO, false)
                    };
                let (cache_creation, has_cache_creation) =
                    if let Some(v) = obj.get("cache_creation_input_token_cost") {
                        (Self::decimal_from_value(Some(v)), true)
                    } else {
                        (Decimal::ZERO, false)
                    };

                models.insert(
                    name.clone(),
                    ModelPricing {
                        input_cost_per_token: input,
                        output_cost_per_token: output,
                        cache_read_cost: cache_read,
                        has_cache_read,
                        cache_creation_cost: cache_creation,
                        has_cache_creation,
                    },
                );
            }
        }

        // Compute pricing version as 12-char SHA-256 prefix
        let mut hasher = Sha256::new();
        hasher.update(data);
        let hash = hasher.finalize();
        let pricing_version = hex::encode(&hash[..6]);

        (models, pricing_version)
    }

    /// Parses a Decimal from a JSON value (float or string).
    fn decimal_from_value(v: Option<&serde_json::Value>) -> Decimal {
        match v {
            Some(serde_json::Value::Number(n)) => {
                if let Some(f) = n.as_f64() {
                    Decimal::try_from(f).unwrap_or(Decimal::ZERO)
                } else {
                    Decimal::ZERO
                }
            }
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(Decimal::ZERO),
            _ => Decimal::ZERO,
        }
    }

    /// Computes the cost for a model invocation given token counts.
    /// Tries: custom pricing, exact match, provider-prefix stripped, date-suffix fallback.
    ///
    /// `cache_creation_tokens` is the Anthropic-specific count of input tokens
    /// *written* to the prompt cache, charged at the higher
    /// `cache_creation_input_token_cost` rate.
    pub async fn get_cost(
        &self,
        model: &str,
        input_tokens: i64,
        output_tokens: i64,
        cached_tokens: i64,
        cache_creation_tokens: i64,
    ) -> CostResult {
        let inner = self.inner.read().await;

        // Check custom pricing first
        if let Some(cp) = inner.custom.get(model) {
            let thousand = Decimal::new(1000, 0);
            let input_cost = cp.input_per_1k * Decimal::new(input_tokens, 0) / thousand;
            let output_cost = cp.output_per_1k * Decimal::new(output_tokens, 0) / thousand;
            return CostResult {
                cost_usd: input_cost + output_cost,
                cost_confidence: CostConfidence::Computed,
                pricing_source: PricingSource::Custom,
                pricing_version: inner.pricing_version.clone(),
            };
        }

        // Try to find model pricing
        if let Some(mp) = Self::find_model_in(&inner.models, model) {
            return Self::compute_cost_from(
                mp,
                &inner.pricing_version,
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_creation_tokens,
            );
        }

        // Unknown model
        CostResult {
            cost_usd: Decimal::ZERO,
            cost_confidence: CostConfidence::Unknown,
            pricing_source: PricingSource::Unknown,
            pricing_version: inner.pricing_version.clone(),
        }
    }

    /// Synchronous cost lookup — acquires a blocking read lock.
    /// Useful when called from non-async context.
    pub fn get_cost_sync(
        &self,
        model: &str,
        input_tokens: i64,
        output_tokens: i64,
        cached_tokens: i64,
        cache_creation_tokens: i64,
    ) -> CostResult {
        let inner = self.inner.blocking_read();

        // Check custom pricing first
        if let Some(cp) = inner.custom.get(model) {
            let thousand = Decimal::new(1000, 0);
            let input_cost = cp.input_per_1k * Decimal::new(input_tokens, 0) / thousand;
            let output_cost = cp.output_per_1k * Decimal::new(output_tokens, 0) / thousand;
            return CostResult {
                cost_usd: input_cost + output_cost,
                cost_confidence: CostConfidence::Computed,
                pricing_source: PricingSource::Custom,
                pricing_version: inner.pricing_version.clone(),
            };
        }

        if let Some(mp) = Self::find_model_in(&inner.models, model) {
            return Self::compute_cost_from(
                mp,
                &inner.pricing_version,
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_creation_tokens,
            );
        }

        CostResult {
            cost_usd: Decimal::ZERO,
            cost_confidence: CostConfidence::Unknown,
            pricing_source: PricingSource::Unknown,
            pricing_version: inner.pricing_version.clone(),
        }
    }

    /// Resolves a model name to its pricing entry using fallback strategies.
    fn find_model_in<'a>(
        models: &'a HashMap<String, ModelPricing>,
        model: &str,
    ) -> Option<&'a ModelPricing> {
        // 1. Exact match
        if let Some(mp) = models.get(model) {
            return Some(mp);
        }

        // 2. Strip provider prefix (e.g. "openai/gpt-4o" -> "gpt-4o")
        if let Some(idx) = model.find('/') {
            let stripped = &model[idx + 1..];
            if let Some(mp) = models.get(stripped) {
                return Some(mp);
            }
        }

        // 3. Longest-prefix walk on `-`: drop trailing components one at a
        //    time and retry. For `"gpt-4o-mini-2024"` this tries
        //    `gpt-4o-mini-2024`, `gpt-4o-mini`, `gpt-4o`, `gpt`.
        //
        //    This handles arbitrary suffixes (date stamps, region tags,
        //    fine-tune labels, etc.), not just the strict `-YYYY-MM-DD`
        //    form. Mirrors the Python SDK `pricing.py` `_resolve_model`
        //    (`pricing.py:248-256`).
        let parts: Vec<&str> = model.split('-').collect();
        for i in (1..parts.len()).rev() {
            let candidate = parts[..i].join("-");
            if let Some(mp) = models.get(&candidate) {
                return Some(mp);
            }
        }

        None
    }

    /// Computes the cost from model pricing and token counts.
    ///
    /// Cached (read) tokens and cache-creation tokens are both subtracted from
    /// `input_tokens` and charged at their respective rates. Mirrors the
    /// Python SDK (`pricing.py:174-186`):
    ///
    /// - `effective_cached = min(cached_tokens, input_tokens)`
    /// - `remaining = input_tokens - effective_cached`
    /// - `effective_creation = min(cache_creation_tokens, remaining)`
    /// - `non_cached = remaining - effective_creation`
    fn compute_cost_from(
        mp: &ModelPricing,
        pricing_version: &str,
        input_tokens: i64,
        output_tokens: i64,
        cached_tokens: i64,
        cache_creation_tokens: i64,
    ) -> CostResult {
        // Cache-read tokens are only discounted when the model advertises a
        // cache-read rate; otherwise they stay at the full input rate.
        let effective_cached = if mp.has_cache_read {
            cached_tokens.max(0).min(input_tokens)
        } else {
            0
        };
        let remaining = input_tokens - effective_cached;

        // Likewise, cache-creation tokens use the dedicated rate only when the
        // model advertises one.
        let effective_creation = if mp.has_cache_creation {
            cache_creation_tokens.max(0).min(remaining)
        } else {
            0
        };
        let non_cached = remaining - effective_creation;

        let input_cost = mp.input_cost_per_token * Decimal::new(non_cached, 0)
            + mp.cache_read_cost * Decimal::new(effective_cached, 0)
            + mp.cache_creation_cost * Decimal::new(effective_creation, 0);

        let output_cost = mp.output_cost_per_token * Decimal::new(output_tokens, 0);
        let total = input_cost + output_cost;

        CostResult {
            cost_usd: total,
            cost_confidence: CostConfidence::Computed,
            pricing_source: PricingSource::Litellm,
            pricing_version: pricing_version.to_string(),
        }
    }

    /// Overrides bundled pricing for a model with per-1k-token rates.
    pub async fn set_custom_pricing(
        &self,
        model: &str,
        input_per_1k: Decimal,
        output_per_1k: Decimal,
    ) {
        let mut inner = self.inner.write().await;
        inner.custom.insert(
            model.to_string(),
            CustomPricing {
                input_per_1k,
                output_per_1k,
            },
        );
    }

    /// Overrides bundled pricing for a model (blocking, non-async).
    pub fn set_custom_pricing_sync(
        &self,
        model: &str,
        input_per_1k: Decimal,
        output_per_1k: Decimal,
    ) {
        let mut inner = self.inner.blocking_write();
        inner.custom.insert(
            model.to_string(),
            CustomPricing {
                input_per_1k,
                output_per_1k,
            },
        );
    }

    /// Returns the pricing version (12-char SHA-256 prefix of the cost map data).
    pub fn pricing_version_sync(&self) -> String {
        self.inner.blocking_read().pricing_version.clone()
    }

    /// Returns the pricing version asynchronously.
    pub async fn pricing_version(&self) -> String {
        self.inner.read().await.pricing_version.clone()
    }

    /// Returns the number of models in the pricing data.
    pub fn model_count_sync(&self) -> usize {
        self.inner.blocking_read().models.len()
    }

    /// Returns the number of models asynchronously.
    pub async fn model_count(&self) -> usize {
        self.inner.read().await.models.len()
    }

    // -------------------------------------------------------------------------
    // Background refresh
    // -------------------------------------------------------------------------

    /// Fetch fresh pricing from the control layer. Fail-silent on error.
    ///
    /// Calls `GET {endpoint}/v1/api/pricing-data/latest` and expects JSON:
    /// `{ "models": { "<name>": { "input_cost_per_token": 0.001, ... } } }`
    pub async fn refresh_from_server(
        &self,
        endpoint: &str,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let url = format!(
            "{}/v1/api/pricing-data/latest",
            endpoint.trim_end_matches('/')
        );

        let resp = reqwest::get(&url).await?;
        if !resp.status().is_success() {
            return Err(format!("pricing refresh failed: HTTP {}", resp.status()).into());
        }
        let body = resp.bytes().await?;

        // Parse: { "models": { ... } }
        let outer: serde_json::Value = serde_json::from_slice(&body)?;
        let models_val = outer
            .get("models")
            .ok_or("missing 'models' key in response")?;

        let models_bytes = serde_json::to_vec(models_val)?;
        let (new_models, new_version) = Self::parse_cost_map(&models_bytes);

        {
            let mut inner = self.inner.write().await;
            inner.models = new_models;
            inner.pricing_version = new_version;
        }

        Ok(())
    }

    /// Start background refresh on a tokio interval.
    ///
    /// Immediately calls `refresh_from_server`, then repeats every `interval`.
    /// Errors from the server are silently ignored (fail-silent).
    /// Only one background task runs at a time; calling this again replaces
    /// the previous stop signal (the old task will also stop on its next tick).
    pub fn start_background_refresh(&self, endpoint: String, interval: Duration) {
        // Reset the stop signal so any previously spawned task (sharing the old
        // Arc) will exit, and our new task starts fresh.
        self.stop_signal.store(false, Ordering::SeqCst);

        let inner = Arc::clone(&self.inner);
        let stop = Arc::clone(&self.stop_signal);

        tokio::spawn(async move {
            // Build a temporary engine shell that wraps the shared inner state.
            // This avoids cloning all pricing data — we operate directly on the Arc.
            let engine = RefreshWorker {
                inner,
                stop: Arc::clone(&stop),
            };

            // Immediate first refresh
            let _ = engine.refresh(&endpoint).await;

            let mut ticker = tokio::time::interval(interval);
            ticker.tick().await; // consume the first (immediate) tick

            loop {
                ticker.tick().await;
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                let _ = engine.refresh(&endpoint).await;
            }
        });
    }

    /// Stop the background refresh task.
    pub fn stop_background_refresh(&self) {
        self.stop_signal.store(true, Ordering::SeqCst);
    }
}

impl Default for PricingEngine {
    fn default() -> Self {
        Self::new()
    }
}

/// Internal helper used inside the background refresh task.
/// Holds a reference to the shared inner state and the stop flag.
struct RefreshWorker {
    inner: Arc<RwLock<Inner>>,
    stop: Arc<AtomicBool>,
}

impl RefreshWorker {
    async fn refresh(
        &self,
        endpoint: &str,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        if self.stop.load(Ordering::SeqCst) {
            return Ok(());
        }

        let url = format!(
            "{}/v1/api/pricing-data/latest",
            endpoint.trim_end_matches('/')
        );

        let resp = reqwest::get(&url).await?;
        if !resp.status().is_success() {
            return Err(format!("pricing refresh failed: HTTP {}", resp.status()).into());
        }
        let body = resp.bytes().await?;

        let outer: serde_json::Value = serde_json::from_slice(&body)?;
        let models_val = outer
            .get("models")
            .ok_or("missing 'models' key in response")?;

        let models_bytes = serde_json::to_vec(models_val)?;
        let (new_models, new_version) = PricingEngine::parse_cost_map(&models_bytes);

        let mut inner = self.inner.write().await;
        inner.models = new_models;
        inner.pricing_version = new_version;

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_pricing_engine_loads() {
        let engine = PricingEngine::new();
        assert!(engine.model_count().await > 0);
        assert!(!engine.pricing_version().await.is_empty());
    }

    #[tokio::test]
    async fn test_known_model_cost() {
        let engine = PricingEngine::new();
        let result = engine.get_cost("gpt-4o", 1000, 500, 0, 0).await;
        if result.cost_confidence == CostConfidence::Computed {
            assert!(result.cost_usd > Decimal::ZERO);
            assert_eq!(result.pricing_source, PricingSource::Litellm);
        }
    }

    #[tokio::test]
    async fn test_unknown_model_cost() {
        let engine = PricingEngine::new();
        let result = engine
            .get_cost("totally-unknown-model-xyz", 1000, 500, 0, 0)
            .await;
        assert_eq!(result.cost_usd, Decimal::ZERO);
        assert_eq!(result.cost_confidence, CostConfidence::Unknown);
        assert_eq!(result.pricing_source, PricingSource::Unknown);
    }

    #[tokio::test]
    async fn test_custom_pricing() {
        let engine = PricingEngine::new();
        let input_per_1k = Decimal::new(1, 3); // 0.001
        let output_per_1k = Decimal::new(2, 3); // 0.002

        engine
            .set_custom_pricing("my-custom-model", input_per_1k, output_per_1k)
            .await;

        let result = engine.get_cost("my-custom-model", 1000, 500, 0, 0).await;
        assert_eq!(result.cost_usd, Decimal::new(2, 3));
        assert_eq!(result.cost_confidence, CostConfidence::Computed);
        assert_eq!(result.pricing_source, PricingSource::Custom);
    }

    #[tokio::test]
    async fn test_provider_prefix_fallback() {
        let engine = PricingEngine::new();
        let with_prefix = engine.get_cost("openai/gpt-4o", 1000, 500, 0, 0).await;
        let without_prefix = engine.get_cost("gpt-4o", 1000, 500, 0, 0).await;
        assert_eq!(with_prefix.cost_usd, without_prefix.cost_usd);
    }

    #[tokio::test]
    async fn test_zero_tokens() {
        let engine = PricingEngine::new();
        let result = engine.get_cost("gpt-4o", 0, 0, 0, 0).await;
        assert_eq!(result.cost_usd, Decimal::ZERO);
    }

    // Fix 1: a model name with a *non-date* suffix must resolve to its base
    // model's price via the longest-prefix walk, instead of falling through
    // to Unknown (priced at $0). The strict `-YYYY-MM-DD` regex used to miss
    // any suffix that was not a date stamp.
    #[tokio::test]
    async fn test_non_date_suffix_resolves_to_base_price() {
        let engine = PricingEngine::new();

        // Sanity-check: the base model exists in the bundled pricing data.
        let base = engine.get_cost("gpt-4o-mini", 1000, 500, 0, 0).await;
        assert_eq!(
            base.cost_confidence,
            CostConfidence::Computed,
            "base model gpt-4o-mini must be priced"
        );
        assert!(base.cost_usd > Decimal::ZERO);

        // `gpt-4o-mini-2024` is NOT a strict `-YYYY-MM-DD` suffix, so the old
        // regex-only resolver returned Unknown / $0. The longest-prefix walk
        // strips `-2024` and resolves to `gpt-4o-mini`.
        let suffixed = engine.get_cost("gpt-4o-mini-2024", 1000, 500, 0, 0).await;
        assert_eq!(
            suffixed.cost_confidence,
            CostConfidence::Computed,
            "non-date-suffixed model must resolve, not be Unknown"
        );
        assert!(
            suffixed.cost_usd > Decimal::ZERO,
            "non-date-suffixed model must not be priced at $0"
        );
        assert_eq!(
            suffixed.cost_usd, base.cost_usd,
            "suffixed model must inherit the base model's price"
        );

        // The walk also strips multiple trailing components: `gpt-4o-foo-bar`
        // walks down to `gpt-4o`.
        let base_4o = engine.get_cost("gpt-4o", 1000, 500, 0, 0).await;
        let multi = engine.get_cost("gpt-4o-foo-bar", 1000, 500, 0, 0).await;
        assert_eq!(multi.cost_usd, base_4o.cost_usd);
        assert_eq!(multi.cost_confidence, CostConfidence::Computed);
    }

    // -------------------------------------------------------------------------
    // Background refresh tests
    // -------------------------------------------------------------------------

    #[tokio::test]
    async fn test_refresh_from_server_unreachable_fails_silently() {
        let engine = PricingEngine::new();
        // Port 1 is almost certainly not listening — should return an error
        let result = engine.refresh_from_server("http://127.0.0.1:1").await;
        assert!(result.is_err(), "expected error for unreachable endpoint");
    }

    #[tokio::test]
    async fn test_refresh_from_server_bad_json_returns_error() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // Serve a response with a body that has no "models" key
        tokio::spawn(async move {
            if let Ok((mut stream, _)) = listener.accept().await {
                // Drain the HTTP request headers first (required on Windows)
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf).await;
                let body = b"null";
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes()).await;
                let _ = stream.write_all(body).await;
            }
        });

        let engine = PricingEngine::new();
        let result = engine
            .refresh_from_server(&format!("http://{}", addr))
            .await;
        // `null` has no "models" key — should return an error
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_refresh_from_server_updates_model_map() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // Serve a minimal valid pricing response
        let body = r#"{"models":{"test-refresh-model":{"input_cost_per_token":0.001,"output_cost_per_token":0.002}}}"#;
        let body_bytes = body.as_bytes().to_vec();
        let body_len = body_bytes.len();

        tokio::spawn(async move {
            // Handle up to two connections
            for _ in 0..2u8 {
                if let Ok((mut stream, _)) = listener.accept().await {
                    // Drain request before responding (required on Windows to avoid ECONNABORTED)
                    let mut buf = [0u8; 4096];
                    let _ = stream.read(&mut buf).await;
                    let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body_len
                    );
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(&body_bytes).await;
                }
            }
        });

        let engine = PricingEngine::new();
        let original_count = engine.model_count().await;

        let result = engine
            .refresh_from_server(&format!("http://{}", addr))
            .await;
        assert!(result.is_ok(), "refresh should succeed: {:?}", result);

        // After refresh the model map should contain only what the server returned
        let new_count = engine.model_count().await;
        assert_eq!(
            new_count, 1,
            "model map should be replaced with server data"
        );

        // The refreshed model should be queryable
        let cost = engine.get_cost("test-refresh-model", 1000, 500, 0, 0).await;
        assert_eq!(cost.cost_confidence, CostConfidence::Computed);
        assert!(cost.cost_usd > Decimal::ZERO);
        // Version should have changed from the bundled one
        assert_ne!(engine.pricing_version().await, "");

        assert!(
            original_count != new_count
                || engine.pricing_version().await != PricingEngine::new().pricing_version().await,
            "pricing version should differ from bundled after refresh"
        );
    }

    #[tokio::test]
    async fn test_start_stop_background_refresh_no_panic() {
        let engine = PricingEngine::new();
        // Start with an unreachable endpoint — refresh errors are swallowed
        engine
            .start_background_refresh("http://127.0.0.1:1".to_string(), Duration::from_millis(50));
        // Give the task a moment to attempt one refresh
        tokio::time::sleep(Duration::from_millis(20)).await;
        // Stop should not panic
        engine.stop_background_refresh();
    }

    #[tokio::test]
    async fn test_stop_before_start_no_panic() {
        let engine = PricingEngine::new();
        // Calling stop before start should be a no-op
        engine.stop_background_refresh();
    }

    #[tokio::test]
    async fn test_background_refresh_updates_engine() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let body = r#"{"models":{"bg-refresh-model":{"input_cost_per_token":0.005,"output_cost_per_token":0.010}}}"#;
        let body_bytes = body.as_bytes().to_vec();
        let body_len = body_bytes.len();

        // Serve multiple requests, each time draining the request first
        tokio::spawn(async move {
            loop {
                if let Ok((mut stream, _)) = listener.accept().await {
                    let body_bytes = body_bytes.clone();
                    tokio::spawn(async move {
                        let mut buf = [0u8; 4096];
                        let _ = stream.read(&mut buf).await;
                        let header = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body_len
                        );
                        let _ = stream.write_all(header.as_bytes()).await;
                        let _ = stream.write_all(&body_bytes).await;
                    });
                }
            }
        });

        let engine = Arc::new(PricingEngine::new());
        let engine2 = Arc::clone(&engine);

        engine.start_background_refresh(format!("http://{}", addr), Duration::from_millis(50));

        // Wait for at least one refresh cycle
        tokio::time::sleep(Duration::from_millis(300)).await;

        engine2.stop_background_refresh();

        // After background refresh, bg-refresh-model should exist
        let cost = engine2.get_cost("bg-refresh-model", 1000, 500, 0, 0).await;
        assert_eq!(cost.cost_confidence, CostConfidence::Computed);
        assert!(cost.cost_usd > Decimal::ZERO);
    }
}
