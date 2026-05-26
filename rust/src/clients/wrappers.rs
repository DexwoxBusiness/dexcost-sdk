//! Client wrapper helpers for recording LLM cost events from provider response maps.
//!
//! These helpers work with plain `serde_json::Value` response maps — no actual
//! OpenAI or Anthropic SDK dependency is required. They are the equivalent of
//! Python's `dexcost.clients.tracked_*` map-style helpers.

use serde_json::Value;
use std::collections::HashMap;
use std::sync::LazyLock;

use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource};
use crate::pricing::engine::PricingEngine;
use crate::pricing::rates::RateRegistry;
use crate::pricing::service_catalog::ServiceCatalog;
use crate::security::redaction::scrub_url;
use crate::transport::buffer::EventBuffer;

/// Convenience type for the boxed error returned by the `record_*` helpers.
type RecordError = Box<dyn std::error::Error + Send + Sync>;

/// Record an LLM cost event from an OpenAI-style response map.
///
/// Extracts:
/// - `response["model"]` — model name
/// - `response["usage"]["prompt_tokens"]` — input tokens
/// - `response["usage"]["completion_tokens"]` — output tokens
/// - `response["usage"]["prompt_tokens_details"]["cached_tokens"]` — cached tokens (optional)
///
/// Returns `Err` if `model` or `usage` fields are missing/invalid.
pub async fn record_openai_response(
    buffer: &mut EventBuffer,
    pricing: &PricingEngine,
    task_id: &str,
    response: &Value,
) -> Result<CostEvent, RecordError> {
    let model = response
        .get("model")
        .and_then(|v| v.as_str())
        .ok_or("missing 'model' field in OpenAI response")?;

    let usage = response
        .get("usage")
        .ok_or("missing 'usage' field in OpenAI response")?;

    let input_tokens = usage
        .get("prompt_tokens")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usage.prompt_tokens' in OpenAI response")?;

    let output_tokens = usage
        .get("completion_tokens")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usage.completion_tokens' in OpenAI response")?;

    let cached_tokens = usage
        .get("prompt_tokens_details")
        .and_then(|d| d.get("cached_tokens"))
        .and_then(|v| v.as_i64());

    let cost_result = pricing
        .get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens.unwrap_or(0),
            0,
        )
        .await;

    let mut event = CostEvent::new(task_id, EventType::LlmCall);
    event.provider = Some("openai".to_string());
    event.model = Some(model.to_string());
    event.input_tokens = Some(input_tokens);
    event.output_tokens = Some(output_tokens);
    event.cached_tokens = cached_tokens;
    event.cost_usd = cost_result.cost_usd;
    event.cost_confidence = cost_result.cost_confidence;
    event.pricing_source = Some(cost_result.pricing_source);

    buffer.add_event(event.clone());

    Ok(event)
}

/// Record an LLM cost event from an Anthropic-style response map.
///
/// Extracts:
/// - `response["model"]` — model name
/// - `response["usage"]["input_tokens"]` — input tokens
/// - `response["usage"]["output_tokens"]` — output tokens
/// - `response["usage"]["cache_read_input_tokens"]` — cached input tokens (optional)
///
/// Returns `Err` if `model` or `usage` fields are missing/invalid.
pub async fn record_anthropic_response(
    buffer: &mut EventBuffer,
    pricing: &PricingEngine,
    task_id: &str,
    response: &Value,
) -> Result<CostEvent, RecordError> {
    let model = response
        .get("model")
        .and_then(|v| v.as_str())
        .ok_or("missing 'model' field in Anthropic response")?;

    let usage = response
        .get("usage")
        .ok_or("missing 'usage' field in Anthropic response")?;

    let input_tokens = usage
        .get("input_tokens")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usage.input_tokens' in Anthropic response")?;

    let output_tokens = usage
        .get("output_tokens")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usage.output_tokens' in Anthropic response")?;

    let cached_tokens = usage
        .get("cache_read_input_tokens")
        .and_then(|v| v.as_i64());

    let cache_creation_tokens = usage
        .get("cache_creation_input_tokens")
        .and_then(|v| v.as_i64());

    let cost_result = pricing
        .get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens.unwrap_or(0),
            cache_creation_tokens.unwrap_or(0),
        )
        .await;

    let mut event = CostEvent::new(task_id, EventType::LlmCall);
    event.provider = Some("anthropic".to_string());
    event.model = Some(model.to_string());
    event.input_tokens = Some(input_tokens);
    event.output_tokens = Some(output_tokens);
    event.cached_tokens = cached_tokens;
    event.cost_usd = cost_result.cost_usd;
    event.cost_confidence = cost_result.cost_confidence;
    event.pricing_source = Some(cost_result.pricing_source);

    buffer.add_event(event.clone());

    Ok(event)
}

/// Record an LLM cost event from a Google Gemini response map.
///
/// Gemini uses `usageMetadata` (camelCase) with field names that differ from
/// OpenAI/Anthropic. Extracts:
/// - `response["model"]` — model name (Gemini SDK responses include this)
/// - `response["usageMetadata"]["promptTokenCount"]` — input tokens
/// - `response["usageMetadata"]["candidatesTokenCount"]` — output tokens
/// - `response["usageMetadata"]["cachedContentTokenCount"]` — cached tokens (optional)
///
/// If `model` is absent the caller can pass it as part of the response under
/// the `model` key (the helper does not infer model from URL).
pub async fn record_gemini_response(
    buffer: &mut EventBuffer,
    pricing: &PricingEngine,
    task_id: &str,
    response: &Value,
) -> Result<CostEvent, RecordError> {
    let model = response
        .get("model")
        .and_then(|v| v.as_str())
        .ok_or("missing 'model' field in Gemini response")?;

    let usage = response
        .get("usageMetadata")
        .ok_or("missing 'usageMetadata' field in Gemini response")?;

    let input_tokens = usage
        .get("promptTokenCount")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usageMetadata.promptTokenCount' in Gemini response")?;

    let output_tokens = usage
        .get("candidatesTokenCount")
        .and_then(|v| v.as_i64())
        .ok_or("missing or invalid 'usageMetadata.candidatesTokenCount' in Gemini response")?;

    let cached_tokens = usage
        .get("cachedContentTokenCount")
        .and_then(|v| v.as_i64());

    let cost_result = pricing
        .get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens.unwrap_or(0),
            0,
        )
        .await;

    let mut event = CostEvent::new(task_id, EventType::LlmCall);
    event.provider = Some("google".to_string());
    event.model = Some(model.to_string());
    event.input_tokens = Some(input_tokens);
    event.output_tokens = Some(output_tokens);
    event.cached_tokens = cached_tokens;
    event.cost_usd = cost_result.cost_usd;
    event.cost_confidence = cost_result.cost_confidence;
    event.pricing_source = Some(cost_result.pricing_source);

    buffer.add_event(event.clone());

    Ok(event)
}

/// Record an LLM cost event from a LiteLLM response map.
///
/// LiteLLM normalises responses to the OpenAI shape but the `model` field is
/// provider-prefixed (e.g. `"openai/gpt-4o"`, `"anthropic/claude-3-5-sonnet"`).
/// This helper delegates to [`record_openai_response`] for parsing but stamps
/// `event.provider = "litellm"` so downstream analytics can distinguish
/// LiteLLM-routed calls from direct provider calls.
pub async fn record_litellm_response(
    buffer: &mut EventBuffer,
    pricing: &PricingEngine,
    task_id: &str,
    response: &Value,
) -> Result<CostEvent, RecordError> {
    // LiteLLM normalises to the OpenAI response shape; reuse the OpenAI helper
    // for parsing and aggregation, then re-stamp the provider field on the
    // already-persisted row via `update_event`.
    let mut event = record_openai_response(buffer, pricing, task_id, response).await?;
    event.provider = Some("litellm".to_string());
    buffer.update_event(&event);
    Ok(event)
}

/// Record a cost event for an MCP (Model Context Protocol) server call.
///
/// MCP servers are non-LLM external services. The cost is derived from the
/// service catalog (`ServiceCatalog::lookup`) when the server URL matches a
/// known entry; otherwise an event with [`CostConfidence::Unknown`] and zero
/// cost is recorded so the caller can attribute the call later via the rate
/// registry.
///
/// Returns the recorded event.
pub fn record_mcp_response(
    buffer: &mut EventBuffer,
    catalog: &ServiceCatalog,
    task_id: &str,
    server_url: &str,
    response_size_bytes: Option<u64>,
) -> CostEvent {
    let mut event = CostEvent::new(task_id, EventType::ExternalCost);
    event.provider = Some("mcp".to_string());
    event.service_name = Some(scrub_url(server_url));

    if let Some(size) = response_size_bytes {
        event.details.insert(
            "response_size_bytes".to_string(),
            serde_json::Value::Number(serde_json::Number::from(size)),
        );
    }

    // Try to extract cost from the catalog by URL.
    if let Some(entry) = catalog.lookup(server_url) {
        if let Some(extracted) =
            catalog.extract_cost(entry, &std::collections::HashMap::new(), None)
        {
            event.cost_usd = extracted.amount;
            event.service_name = Some(entry.display_name.clone());
            event.cost_confidence = match extracted.confidence.as_str() {
                "exact" => CostConfidence::Exact,
                "computed" => CostConfidence::Computed,
                "estimated" => CostConfidence::Estimated,
                _ => CostConfidence::Unknown,
            };
            event.pricing_source = Some(PricingSource::ServiceCatalog);
        } else {
            event.service_name = Some(entry.display_name.clone());
            event.cost_confidence = CostConfidence::Unknown;
            event.pricing_source = Some(PricingSource::ServiceCatalog);
        }
    } else {
        event.cost_confidence = CostConfidence::Unknown;
        event.pricing_source = Some(PricingSource::RateRegistry);
    }

    buffer.add_event(event.clone());
    event
}

// ---------------------------------------------------------------------------
// MCP tool-name-to-catalog-key map (mirrors Python/Go/TypeScript SDKs)
// ---------------------------------------------------------------------------

static MCP_TOOL_MAP: LazyLock<HashMap<&'static str, &'static str>> = LazyLock::new(|| {
    HashMap::from([
        // Search
        ("tavily_search", "tavily_search"), ("tavily_extract", "tavily_search"),
        ("tavily_crawl", "tavily_search"), ("tavily_map", "tavily_search"),
        ("serper_search", "serper_search"), ("serper_google_search", "serper_search"),
        ("exa_search", "exa_search"), ("exa_find_similar", "exa_search"),
        ("exa_get_contents", "exa_search"), ("perplexity_search", "perplexity_search"),
        ("sonar_search", "perplexity_search"), ("brave_web_search", "brave_search"),
        ("brave_local_search", "brave_search"), ("brave_search", "brave_search"),
        ("bing_search", "bing_search"), ("bing_web_search", "bing_search"),
        ("serpapi_search", "serpapi"), ("serpapi_google_search", "serpapi"),
        // Scraping
        ("firecrawl_scrape", "firecrawl_scrape"), ("firecrawl_crawl", "firecrawl_scrape"),
        ("firecrawl_map", "firecrawl_scrape"), ("firecrawl_extract", "firecrawl_scrape"),
        ("browserbase_create_session", "browserbase"), ("browserbase_navigate", "browserbase"),
        ("scrapingbee_scrape", "scrapingbee"), ("apify_run_actor", "apify"),
        ("diffbot_analyze", "diffbot"), ("diffbot_extract", "diffbot"),
        ("jina_read", "jina_reader"), ("jina_reader", "jina_reader"),
        ("scraperapi_scrape", "scraperapi"), ("zenrows_scrape", "zenrows"),
        ("oxylabs_scrape", "oxylabs_scraper"),
        // Vector DBs
        ("pinecone_query", "pinecone_query"), ("pinecone_upsert", "pinecone_query"),
        ("weaviate_query", "weaviate_cloud"), ("weaviate_search", "weaviate_cloud"),
        ("qdrant_search", "qdrant_cloud"), ("qdrant_query", "qdrant_cloud"),
        ("milvus_search", "milvus_zilliz"), ("astra_query", "astra_db"),
        ("chroma_query", "chroma_cloud"), ("chroma_add", "chroma_cloud"),
        ("turbopuffer_query", "turbopuffer"),
        // Compute
        ("e2b_run_code", "e2b_sandbox"), ("e2b_execute", "e2b_sandbox"),
        ("modal_run", "modal_compute"), ("lambda_invoke", "aws_lambda"),
        ("fly_machine_run", "fly_machines"), ("cloudflare_worker_run", "cloudflare_workers"),
        // Communication
        ("twilio_send_sms", "twilio_sms"), ("sendgrid_send_email", "sendgrid_email"),
        ("resend_send_email", "resend_email"), ("mailgun_send", "mailgun"),
        ("postmark_send", "postmark"),
        // Messaging
        ("slack_post_message", "slack_api"), ("slack_send_message", "slack_api"),
        ("discord_send_message", "discord_api"), ("telegram_send_message", "telegram_api"),
        ("twilio_call", "twilio_voice"), ("onesignal_send", "onesignal"),
        ("pusher_trigger", "pusher"), ("novu_trigger", "novu"),
        // Maps
        ("google_maps_geocode", "google_maps_geocode"), ("google_maps_directions", "google_maps_directions"),
        ("google_maps_places", "google_maps_places"), ("mapbox_geocode", "mapbox_geocoding"),
        // Data Enrichment
        ("people_search", "people_data_labs"), ("clearbit_enrich", "clearbit_enrichment"),
        ("hunter_search", "hunter_io"), ("crunchbase_search", "crunchbase"),
        ("apollo_search", "apollo_io"), ("apollo_enrich", "apollo_io"),
        ("zoominfo_search", "zoominfo_api"),
        // Payments
        ("stripe_create_charge", "stripe_payment"), ("stripe_create_payment", "stripe_payment"),
        ("paypal_create_payment", "paypal"), ("razorpay_create_order", "razorpay"),
        // Speech
        ("elevenlabs_tts", "eleven_labs"), ("deepgram_transcribe", "deepgram_transcription"),
        ("assemblyai_transcribe", "assemblyai"), ("whisper_transcribe", "openai_whisper"),
        ("openai_tts", "openai_tts"), ("resemble_generate", "resemble_ai"),
        ("playht_generate", "playht"),
        // Image
        ("dalle_generate", "openai_dalle"), ("stability_generate", "stability_ai"),
        ("replicate_run", "replicate"), ("midjourney_generate", "midjourney_api"),
        ("fal_generate", "flux_fal"), ("flux_generate", "flux_fal"),
        ("leonardo_generate", "leonardo_ai"),
        // Document
        ("textract_analyze", "amazon_textract"), ("document_ai_process", "google_document_ai"),
        ("unstructured_parse", "unstructured_io"), ("llamaparse_parse", "llamaparse"),
        ("adobe_pdf_extract", "adobe_pdf_services"), ("docraptor_convert", "docraptor"),
        // Financial
        ("twelve_data_quote", "twelve_data"), ("alpha_vantage_quote", "alpha_vantage"),
        ("polygon_quote", "polygon_io"), ("coinapi_exchange_rate", "coinapi"),
        // Cloud Storage
        ("s3_get_object", "aws_s3"), ("s3_put_object", "aws_s3"),
        ("supabase_query", "supabase"), ("gcs_upload", "gcs"),
        ("r2_put_object", "r2_cloudflare"), ("azure_blob_upload", "azure_blob"),
        // Agent Tools
        ("composio_execute", "composio"), ("wolfram_query", "wolfram_alpha"),
        ("browserless_navigate", "browserless"), ("browserless_scrape", "browserless"),
        // Embeddings
        ("openai_embed", "openai_embeddings"), ("cohere_embed", "cohere_embed"),
        ("voyage_embed", "voyage_ai"), ("jina_embed", "jina_embeddings"),
        ("mixedbread_embed", "mixedbread_embed"), ("nomic_embed", "nomic_embed"),
        // Translation
        ("deepl_translate", "deepl_translate"), ("google_translate", "google_translate"),
        ("aws_translate", "aws_translate"), ("azure_translate", "azure_translator"),
        // Vision
        ("google_vision_annotate", "google_vision"), ("azure_vision_analyze", "azure_computer_vision"),
        ("aws_rekognition_detect", "aws_rekognition"), ("mathpix_process", "mathpix_ocr"),
        // Video AI
        ("runway_generate", "runway_video"), ("heygen_create_video", "heygen_video"),
        ("luma_generate", "luma_video"), ("mux_upload", "mux_video"),
        // Database
        ("neon_query", "neon_postgres"), ("planetscale_query", "planetscale"),
        ("turso_query", "turso_db"), ("mongodb_find", "mongodb_atlas"),
        ("mongodb_query", "mongodb_atlas"), ("upstash_redis_get", "upstash_redis"),
        ("upstash_redis_set", "upstash_redis"), ("fauna_query", "fauna_db"),
        ("cockroach_query", "cockroachdb"),
        // Project Management
        ("jira_create_issue", "jira_api"), ("jira_search", "jira_api"),
        ("linear_create_issue", "linear_api"), ("linear_search", "linear_api"),
        ("asana_create_task", "asana_api"), ("clickup_create_task", "clickup_api"),
        ("notion_query", "notion_api"), ("notion_create_page", "notion_api"),
        ("notion_search", "notion_api"),
        // CRM
        ("salesforce_query", "salesforce_api"), ("hubspot_search", "hubspot_api"),
        // DevOps
        ("github_create_issue", "github_api"), ("github_search", "github_api"),
        ("gitlab_create_issue", "gitlab_api"), ("vercel_deploy", "vercel_api"),
        // Social
        ("twitter_post", "twitter_api"), ("twitter_search", "twitter_api"),
        ("linkedin_post", "linkedin_api"), ("reddit_submit", "reddit_api"),
        ("youtube_search", "youtube_data_api"),
        // Weather/News/Geo
        ("get_weather", "openweathermap"), ("get_news", "newsapi"),
        ("ip_lookup", "ipinfo"),
        // Workflow Automation
        ("zapier_nla", "zapier"), ("zapier_action", "zapier"),
        ("make_webhook", "make_integromat"), ("pipedream_trigger", "pipedream"),
        // E-commerce
        ("shopify_create_product", "shopify_api"), ("shopify_get_orders", "shopify_api"),
        // Monitoring
        ("sentry_capture", "sentry"), ("datadog_submit", "datadog"),
        ("posthog_capture", "posthog"), ("mixpanel_track", "mixpanel"),
        // CDN
        ("cloudinary_upload", "cloudinary"),
        // Reranking
        ("cohere_rerank", "cohere_rerank"), ("jina_rerank", "jina_reranker"),
        ("rerank", "cohere_rerank"),
        // AI/ML
        ("huggingface_inference", "huggingface_inference"), ("together_generate", "together_ai"),
        ("groq_chat", "groq_inference"), ("fireworks_generate", "fireworks_ai"),
        ("roboflow_detect", "roboflow"), ("scale_create_task", "scale_ai"),
        // E-signature/Calendar
        ("docusign_send", "docusign"), ("docusign_create_envelope", "docusign"),
        ("calendly_create_event", "calendly_api"),
        // ── Aliases missing from initial map (parity with Python/Go/TS) ──
        // Search aliases
        ("tavily_research", "tavily_search"),
        // Scraping aliases
        ("firecrawl_search", "firecrawl_scrape"), ("apify_get_dataset", "apify"),
        ("browserbase_screenshot", "browserbase"), ("browserbase_get_content", "browserbase"),
        ("jina_search", "jina_reader"), ("scrapingdog_scrape", "scrapingdog"),
        ("code_execute", "e2b_sandbox"), ("e2b_create_sandbox", "e2b_sandbox"),
        // Vector DB aliases
        ("pinecone_delete", "pinecone_query"), ("pinecone_fetch", "pinecone_query"),
        ("qdrant_upsert", "qdrant_cloud"), ("milvus_query", "milvus_zilliz"),
        ("astra_search", "astra_db"), ("turbopuffer_upsert", "turbopuffer"),
        // Compute aliases
        ("modal_deploy", "modal_compute"),
        // Communication aliases
        ("twilio_send_message", "twilio_sms"), ("send_email", "sendgrid_email"),
        ("twilio_make_call", "twilio_voice"),
        // Messaging aliases
        ("slack_chat_post", "slack_api"), ("discord_create_message", "discord_api"),
        ("telegram_send", "telegram_api"),
        // Maps aliases
        ("google_maps_search", "google_maps_places"), ("mapbox_directions", "mapbox_geocoding"),
        // Data Enrichment aliases
        ("pdl_search", "people_data_labs"), ("pdl_enrich", "people_data_labs"),
        ("clearbit_lookup", "clearbit_enrichment"), ("hunter_email_finder", "hunter_io"),
        ("crunchbase_lookup", "crunchbase"), ("zoominfo_enrich", "zoominfo_api"),
        // Payment aliases
        ("stripe_create_payment_link", "stripe_payment"), ("stripe_list_charges", "stripe_payment"),
        ("paypal_capture", "paypal"), ("razorpay_capture_payment", "razorpay"),
        // Speech aliases
        ("elevenlabs_text_to_speech", "eleven_labs"), ("openai_text_to_speech", "openai_tts"),
        // Image aliases
        ("openai_generate_image", "openai_dalle"), ("replicate_predict", "replicate"),
        // Document aliases
        ("textract_detect", "amazon_textract"), ("llamaparse_upload", "llamaparse"),
        // Financial aliases
        ("twelve_data_time_series", "twelve_data"), ("polygon_aggs", "polygon_io"),
        // Cloud storage aliases
        ("supabase_insert", "supabase"), ("supabase_select", "supabase"),
        ("gcs_download", "gcs"), ("r2_get_object", "r2_cloudflare"),
        // Agent tool aliases
        ("composio_action", "composio"), ("wolfram_alpha_query", "wolfram_alpha"),
        ("browserless_screenshot", "browserless"),
        // Vision aliases
        ("google_vision_ocr", "google_vision"), ("azure_ocr", "azure_computer_vision"),
        ("aws_rekognition_labels", "aws_rekognition"),
        // Video aliases
        ("mux_create_asset", "mux_video"),
        // Database aliases
        ("neon_sql", "neon_postgres"), ("turso_execute", "turso_db"),
        ("mongodb_insert", "mongodb_atlas"), ("upstash_redis_command", "upstash_redis"),
        // Project Management aliases
        ("jira_update_issue", "jira_api"), ("jira_get_issue", "jira_api"),
        ("linear_update_issue", "linear_api"), ("asana_search", "asana_api"),
        ("clickup_search", "clickup_api"), ("notion_update_page", "notion_api"),
        // CRM aliases
        ("salesforce_create", "salesforce_api"), ("salesforce_soql", "salesforce_api"),
        ("hubspot_create_contact", "hubspot_api"), ("hubspot_get_contacts", "hubspot_api"),
        // DevOps aliases
        ("github_create_pr", "github_api"), ("github_get_file", "github_api"),
        ("github_list_repos", "github_api"), ("gitlab_search", "gitlab_api"),
        ("vercel_list_deployments", "vercel_api"),
        // Social aliases
        ("x_post_tweet", "twitter_api"), ("linkedin_share", "linkedin_api"),
        ("reddit_search", "reddit_api"), ("youtube_list_videos", "youtube_data_api"),
        // Weather/News/Geo aliases
        ("openweathermap_current", "openweathermap"), ("openweathermap_forecast", "openweathermap"),
        ("weather_forecast", "weatherapi"), ("news_search", "newsapi"),
        ("geolocate", "maxmind_geoip"),
        // Workflow aliases
        ("translate_text", "deepl_translate"),
        // Monitoring aliases
        ("segment_track", "segment"),
        // CDN aliases
        ("cloudinary_transform", "cloudinary"), ("uploadthing_upload", "uploadthing"),
        // Auth aliases
        ("auth0_get_user", "auth0"), ("clerk_get_user", "clerk_auth"),
        // E-commerce aliases
        ("shopify_get_orders", "shopify_api"),
        // AI/ML aliases
        ("hf_generate", "huggingface_inference"), ("roboflow_classify", "roboflow"),
    ])
});

/// Record a cost event for an MCP tool call identified by **tool name**.
///
/// This is the Rust equivalent of Python's `_call_tool_wrapper`, Go's
/// `RecordMCPResponse`, and TypeScript's MCP instrument. It resolves cost via:
///
/// 1. Rate registry: explicit `mcp:<tool_name>` rate.
/// 2. Tool map → catalog key → rate registry.
/// 3. Catalog key → service_prices.json fixed cost.
/// 4. Fallback: cost=0, confidence=Unknown.
#[allow(clippy::too_many_arguments)]
pub fn record_mcp_tool_call(
    buffer: &mut EventBuffer,
    catalog: &ServiceCatalog,
    rates: Option<&RateRegistry>,
    task_id: &str,
    tool_name: &str,
    server: Option<&str>,
    latency_ms: Option<i64>,
    is_error: bool,
) -> CostEvent {
    let mut event = CostEvent::new(task_id, EventType::ExternalCost);
    event.service_name = Some(format!("mcp:{}", tool_name));

    event.details.insert("mcp_tool".to_string(), Value::String(tool_name.to_string()));
    event.details.insert(
        "mcp_server".to_string(),
        Value::String(scrub_url(server.unwrap_or("unknown"))),
    );
    event.details.insert("is_error".to_string(), Value::Bool(is_error));
    if let Some(ms) = latency_ms {
        event.latency_ms = Some(ms);
        event.details.insert("latency_ms".to_string(), Value::Number(serde_json::Number::from(ms)));
    }

    // 1. Direct rate registry lookup: "mcp:<tool_name>"
    if let Some(rates) = rates {
        let mcp_key = format!("mcp:{}", tool_name);
        if let Some(entry) = rates.get(&mcp_key) {
            event.cost_usd = entry.cost_usd;
            event.cost_confidence = CostConfidence::Computed;
            event.pricing_source = Some(PricingSource::RateRegistry);
            buffer.add_event(event.clone());
            return event;
        }

        // 2. Tool map → catalog key → rate registry
        if let Some(&catalog_key) = MCP_TOOL_MAP.get(tool_name) {
            if let Some(entry) = rates.get(catalog_key) {
                event.cost_usd = entry.cost_usd;
                event.cost_confidence = CostConfidence::Computed;
                event.pricing_source = Some(PricingSource::RateRegistry);
                buffer.add_event(event.clone());
                return event;
            }
        }
    }

    // 3. Catalog key → service_prices.json fixed cost
    if let Some(&catalog_key) = MCP_TOOL_MAP.get(tool_name) {
        if let Some(entry) = catalog.get_by_key(catalog_key) {
            if let Some(extracted) = catalog.extract_cost(entry, &HashMap::new(), None) {
                event.cost_usd = extracted.amount;
                event.cost_confidence = match extracted.confidence.as_str() {
                    "exact" => CostConfidence::Exact,
                    "computed" => CostConfidence::Computed,
                    "estimated" => CostConfidence::Estimated,
                    _ => CostConfidence::Unknown,
                };
                event.pricing_source = Some(PricingSource::ServiceCatalog);
                buffer.add_event(event.clone());
                return event;
            }
        }
    }

    // 4. Fallback
    event.cost_confidence = CostConfidence::Unknown;
    event.pricing_source = Some(PricingSource::Unknown);
    buffer.add_event(event.clone());
    event
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::models::{CostConfidence, EventType};
    use serde_json::json;

    #[tokio::test]
    async fn test_record_openai_response() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500
            }
        });

        let event = record_openai_response(&mut buffer, &pricing, "task-1", &response)
            .await
            .expect("should succeed");

        assert_eq!(event.task_id, "task-1");
        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("openai"));
        assert_eq!(event.model.as_deref(), Some("gpt-4o"));
        assert_eq!(event.input_tokens, Some(1000));
        assert_eq!(event.output_tokens, Some(500));
        assert!(event.cached_tokens.is_none());
        // Should have been added to the buffer
        assert_eq!(buffer.event_count(), 1);
    }

    #[tokio::test]
    async fn test_record_openai_with_cached_tokens() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 400,
                "prompt_tokens_details": {
                    "cached_tokens": 600
                }
            }
        });

        let event = record_openai_response(&mut buffer, &pricing, "task-c1", &response)
            .await
            .expect("should succeed with cached tokens");
        assert_eq!(event.cached_tokens, Some(600));
        assert_eq!(buffer.event_count(), 1);
    }

    #[tokio::test]
    async fn test_record_anthropic_response() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "claude-3-5-sonnet-20241022",
            "usage": {
                "input_tokens": 800,
                "output_tokens": 300
            }
        });

        let event = record_anthropic_response(&mut buffer, &pricing, "task-2", &response)
            .await
            .expect("should succeed");

        assert_eq!(event.task_id, "task-2");
        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("anthropic"));
        assert_eq!(event.model.as_deref(), Some("claude-3-5-sonnet-20241022"));
        assert_eq!(event.input_tokens, Some(800));
        assert_eq!(event.output_tokens, Some(300));
        assert!(event.cached_tokens.is_none());
        assert_eq!(buffer.event_count(), 1);
    }

    #[tokio::test]
    async fn test_record_anthropic_with_cache_read() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "claude-3-5-sonnet-20241022",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 400,
                "cache_read_input_tokens": 800
            }
        });

        let event = record_anthropic_response(&mut buffer, &pricing, "task-c2", &response)
            .await
            .expect("should succeed with cache_read");
        assert_eq!(event.cached_tokens, Some(800));
    }

    #[tokio::test]
    async fn test_record_openai_missing_model() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        // No "model" key
        let response = json!({
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50
            }
        });

        let result = record_openai_response(&mut buffer, &pricing, "task-3", &response).await;
        assert!(result.is_err(), "should fail when model is missing");
        assert_eq!(buffer.event_count(), 0);
    }

    #[tokio::test]
    async fn test_record_openai_missing_usage() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        // No "usage" key
        let response = json!({
            "model": "gpt-4o"
        });

        let result = record_openai_response(&mut buffer, &pricing, "task-4", &response).await;
        assert!(result.is_err(), "should fail when usage is missing");
        assert_eq!(buffer.event_count(), 0);
    }

    #[tokio::test]
    async fn test_record_openai_unknown_model_returns_zero_cost() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "unknown-model-xyz-9999",
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200
            }
        });

        let event = record_openai_response(&mut buffer, &pricing, "task-5", &response)
            .await
            .expect("should succeed even for unknown model");

        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(buffer.event_count(), 1);
    }

    #[tokio::test]
    async fn test_record_gemini_response() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "gemini-1.5-pro",
            "usageMetadata": {
                "promptTokenCount": 2000,
                "candidatesTokenCount": 800,
                "cachedContentTokenCount": 300,
                "totalTokenCount": 2800
            }
        });

        let event = record_gemini_response(&mut buffer, &pricing, "task-g1", &response)
            .await
            .expect("should succeed");
        assert_eq!(event.provider.as_deref(), Some("google"));
        assert_eq!(event.model.as_deref(), Some("gemini-1.5-pro"));
        assert_eq!(event.input_tokens, Some(2000));
        assert_eq!(event.output_tokens, Some(800));
        assert_eq!(event.cached_tokens, Some(300));
        assert_eq!(buffer.event_count(), 1);
    }

    #[tokio::test]
    async fn test_record_gemini_response_no_cache() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "gemini-1.5-flash",
            "usageMetadata": {
                "promptTokenCount": 500,
                "candidatesTokenCount": 200
            }
        });

        let event = record_gemini_response(&mut buffer, &pricing, "task-g2", &response)
            .await
            .expect("should succeed without cached tokens");
        assert_eq!(event.cached_tokens, None);
    }

    #[tokio::test]
    async fn test_record_gemini_missing_usage_metadata() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "gemini-1.5-pro"
        });
        let result = record_gemini_response(&mut buffer, &pricing, "task-g3", &response).await;
        assert!(result.is_err());
        assert_eq!(buffer.event_count(), 0);
    }

    #[tokio::test]
    async fn test_record_litellm_response_stamps_provider() {
        let mut buffer = EventBuffer::new().unwrap();
        let pricing = PricingEngine::new();

        let response = json!({
            "model": "openai/gpt-4o",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500
            }
        });

        let event = record_litellm_response(&mut buffer, &pricing, "task-l1", &response)
            .await
            .expect("should succeed");
        assert_eq!(event.provider.as_deref(), Some("litellm"));
        assert_eq!(event.model.as_deref(), Some("openai/gpt-4o"));
        assert_eq!(buffer.event_count(), 1);
        // The persisted row in SQLite must also carry the corrected provider.
        let persisted = buffer.query_events("task-l1");
        assert_eq!(persisted.len(), 1);
        assert_eq!(persisted[0].provider.as_deref(), Some("litellm"));
    }

    #[test]
    fn test_record_mcp_response_known_service() {
        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();

        // exa.ai is a fixed-cost entry in the bundled catalog.
        let event = record_mcp_response(
            &mut buffer,
            &catalog,
            "task-mcp-1",
            "https://api.exa.ai/search",
            Some(12_345),
        );

        assert_eq!(event.event_type, EventType::ExternalCost);
        assert_eq!(event.provider.as_deref(), Some("mcp"));
        assert!(event.cost_usd > rust_decimal::Decimal::ZERO);
        assert_eq!(event.pricing_source, Some(PricingSource::ServiceCatalog));
        assert_eq!(
            event.details.get("response_size_bytes"),
            Some(&serde_json::Value::Number(serde_json::Number::from(
                12_345u64
            ))),
        );
        assert_eq!(buffer.event_count(), 1);
    }

    #[test]
    fn test_record_mcp_response_unknown_service() {
        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();

        let event = record_mcp_response(
            &mut buffer,
            &catalog,
            "task-mcp-2",
            "https://unknown.mcp.example.com/jsonrpc",
            None,
        );
        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(event.pricing_source, Some(PricingSource::RateRegistry));
        assert!(event.cost_usd == rust_decimal::Decimal::ZERO);
        assert_eq!(buffer.event_count(), 1);
    }

    // ── record_mcp_tool_call tests ──────────────────────────────────

    #[test]
    fn test_mcp_tool_call_rate_registry_direct_hit() {
        use crate::pricing::rates::RateRegistry;
        use rust_decimal::Decimal;

        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();
        let mut rates = RateRegistry::new();
        rates.register("mcp:tavily_search", "call", Decimal::new(8, 3)); // 0.008

        let event = record_mcp_tool_call(
            &mut buffer, &catalog, Some(&rates),
            "task-tc-1", "tavily_search", Some("tavily-mcp"), Some(42), false,
        );

        assert_eq!(event.cost_usd, Decimal::new(8, 3));
        assert_eq!(event.cost_confidence, CostConfidence::Computed);
        assert_eq!(event.pricing_source, Some(PricingSource::RateRegistry));
        assert_eq!(event.service_name.as_deref(), Some("mcp:tavily_search"));
        assert_eq!(event.latency_ms, Some(42));
        assert_eq!(buffer.event_count(), 1);
    }

    #[test]
    fn test_mcp_tool_call_toolmap_to_registry() {
        use crate::pricing::rates::RateRegistry;
        use rust_decimal::Decimal;

        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();
        let mut rates = RateRegistry::new();
        // Register under catalog key (no mcp: prefix)
        rates.register("tavily_search", "call", Decimal::new(5, 3)); // 0.005

        let event = record_mcp_tool_call(
            &mut buffer, &catalog, Some(&rates),
            "task-tc-2", "tavily_search", None, None, false,
        );

        assert_eq!(event.cost_usd, Decimal::new(5, 3));
        assert_eq!(event.cost_confidence, CostConfidence::Computed);
        assert_eq!(event.pricing_source, Some(PricingSource::RateRegistry));
        assert_eq!(buffer.event_count(), 1);
    }

    #[test]
    fn test_mcp_tool_call_toolmap_to_catalog() {
        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();

        // No rate registry — should fall through to catalog fixed cost.
        // "deepl_translate" maps to catalog key "deepl_translate" which has
        // cost_per_request_usd = "0.000025" in service_prices.json.
        let event = record_mcp_tool_call(
            &mut buffer, &catalog, None,
            "task-tc-3", "deepl_translate", None, None, false,
        );

        assert_eq!(event.pricing_source, Some(PricingSource::ServiceCatalog));
        assert!(event.cost_usd >= rust_decimal::Decimal::ZERO);
        assert_eq!(buffer.event_count(), 1);
    }

    #[test]
    fn test_mcp_tool_call_fallback_unknown() {
        let mut buffer = EventBuffer::new().unwrap();
        let catalog = ServiceCatalog::new();

        let event = record_mcp_tool_call(
            &mut buffer, &catalog, None,
            "task-tc-4", "completely_unknown_tool_xyz", None, Some(100), true,
        );

        assert_eq!(event.cost_usd, rust_decimal::Decimal::ZERO);
        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(event.pricing_source, Some(PricingSource::Unknown));
        assert_eq!(event.details.get("is_error"), Some(&Value::Bool(true)));
        assert_eq!(event.latency_ms, Some(100));
        assert_eq!(buffer.event_count(), 1);
    }
}
