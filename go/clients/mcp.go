package clients

import (
	"fmt"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// MCPToolMap maps well-known MCP tool names to service catalog keys. This is
// the Go counterpart of `_MCP_TOOL_MAP` in `instruments/mcp.py:49`. It covers
// all 163 services in the catalog, with multiple aliases per tool.
//
// MCP server names vary by implementation, so we map common variants
// (official MCP server names, community aliases, snake_case/camelCase).
var MCPToolMap = map[string]string{
	// ── Search ──────────────────────────────────────────────────────────
	"tavily_search":         "tavily_search",
	"tavily_extract":        "tavily_search",
	"tavily_crawl":          "tavily_search",
	"tavily_map":            "tavily_search",
	"tavily_research":       "tavily_search",
	"serper_search":         "serper_search",
	"serper_google_search":  "serper_search",
	"exa_search":            "exa_search",
	"exa_find_similar":      "exa_search",
	"exa_get_contents":      "exa_search",
	"perplexity_search":     "perplexity_search",
	"sonar_search":          "perplexity_search",
	"brave_web_search":      "brave_search",
	"brave_local_search":    "brave_search",
	"brave_search":          "brave_search",
	"bing_search":           "bing_search",
	"bing_web_search":       "bing_search",
	"serpapi_search":        "serpapi",
	"serpapi_google_search": "serpapi",
	// ── Scraping ────────────────────────────────────────────────────────
	"firecrawl_scrape":          "firecrawl_scrape",
	"firecrawl_crawl":           "firecrawl_scrape",
	"firecrawl_map":             "firecrawl_scrape",
	"firecrawl_search":          "firecrawl_scrape",
	"firecrawl_extract":         "firecrawl_scrape",
	"browserbase_create_session": "browserbase",
	"browserbase_navigate":      "browserbase",
	"browserbase_screenshot":    "browserbase",
	"browserbase_get_content":   "browserbase",
	"scrapingbee_scrape":        "scrapingbee",
	"apify_run_actor":           "apify",
	"apify_get_dataset":         "apify",
	"scrapingdog_scrape":        "scrapingdog",
	"diffbot_analyze":           "diffbot",
	"diffbot_extract":           "diffbot",
	"jina_read":                 "jina_reader",
	"jina_reader":               "jina_reader",
	"jina_search":               "jina_reader",
	// ── Vector Databases ────────────────────────────────────────────────
	"pinecone_query":  "pinecone_query",
	"pinecone_upsert": "pinecone_query",
	"pinecone_delete": "pinecone_query",
	"pinecone_fetch":  "pinecone_query",
	"weaviate_query":  "weaviate_cloud",
	"weaviate_search": "weaviate_cloud",
	"qdrant_search":   "qdrant_cloud",
	"qdrant_query":    "qdrant_cloud",
	"qdrant_upsert":   "qdrant_cloud",
	"milvus_search":   "milvus_zilliz",
	"milvus_query":    "milvus_zilliz",
	"astra_query":     "astra_db",
	"astra_search":    "astra_db",
	// ── Compute / Sandbox ───────────────────────────────────────────────
	"e2b_run_code":       "e2b_sandbox",
	"e2b_execute":        "e2b_sandbox",
	"e2b_create_sandbox": "e2b_sandbox",
	"code_execute":       "e2b_sandbox",
	"modal_run":          "modal_compute",
	"modal_deploy":       "modal_compute",
	"lambda_invoke":      "aws_lambda",
	// ── Communication ───────────────────────────────────────────────────
	"twilio_send_sms":     "twilio_sms",
	"twilio_send_message": "twilio_sms",
	"sendgrid_send_email": "sendgrid_email",
	"send_email":          "sendgrid_email",
	"resend_send_email":   "resend_email",
	"mailgun_send":        "mailgun",
	"postmark_send":       "postmark",
	// ── Maps / Geocoding ────────────────────────────────────────────────
	"google_maps_geocode":    "google_maps_geocode",
	"google_maps_search":     "google_maps_places",
	"google_maps_directions": "google_maps_directions",
	"google_maps_places":     "google_maps_places",
	"mapbox_geocode":         "mapbox_geocoding",
	"mapbox_directions":      "mapbox_geocoding",
	// ── Data Enrichment ─────────────────────────────────────────────────
	"people_search":      "people_data_labs",
	"pdl_search":         "people_data_labs",
	"pdl_enrich":         "people_data_labs",
	"clearbit_enrich":    "clearbit_enrichment",
	"clearbit_lookup":    "clearbit_enrichment",
	"hunter_search":      "hunter_io",
	"hunter_email_finder": "hunter_io",
	"crunchbase_search":  "crunchbase",
	"crunchbase_lookup":  "crunchbase",
	// ── Payments ────────────────────────────────────────────────────────
	"stripe_create_charge":       "stripe_payment",
	"stripe_create_payment":      "stripe_payment",
	"stripe_create_payment_link": "stripe_payment",
	"stripe_list_charges":        "stripe_payment",
	// ── Speech / Audio ──────────────────────────────────────────────────
	"elevenlabs_tts":             "eleven_labs",
	"elevenlabs_text_to_speech":  "eleven_labs",
	"deepgram_transcribe":        "deepgram_transcription",
	"assemblyai_transcribe":      "assemblyai",
	"whisper_transcribe":         "openai_whisper",
	// ── Image Generation ────────────────────────────────────────────────
	"dalle_generate":         "openai_dalle",
	"openai_generate_image":  "openai_dalle",
	"stability_generate":     "stability_ai",
	"replicate_run":          "replicate",
	"replicate_predict":      "replicate",
	// ── Document Processing ─────────────────────────────────────────────
	"textract_analyze":     "amazon_textract",
	"textract_detect":      "amazon_textract",
	"document_ai_process":  "google_document_ai",
	"unstructured_parse":   "unstructured_io",
	"llamaparse_parse":     "llamaparse",
	"llamaparse_upload":    "llamaparse",
	// ── Financial Data ──────────────────────────────────────────────────
	"twelve_data_quote":       "twelve_data",
	"twelve_data_time_series": "twelve_data",
	"alpha_vantage_quote":     "alpha_vantage",
	"polygon_quote":           "polygon_io",
	"polygon_aggs":            "polygon_io",
	"coinapi_exchange_rate":   "coinapi",
	// ── Cloud Storage ───────────────────────────────────────────────────
	"s3_get_object":   "aws_s3",
	"s3_put_object":   "aws_s3",
	"supabase_query":  "supabase",
	"supabase_insert": "supabase",
	"supabase_select": "supabase",
	// ── Agent Tools ─────────────────────────────────────────────────────
	"composio_execute":     "composio",
	"composio_action":      "composio",
	"wolfram_query":        "wolfram_alpha",
	"wolfram_alpha_query":  "wolfram_alpha",
	// ── Embeddings ──────────────────────────────────────────────────────
	"openai_embed":      "openai_embeddings",
	"cohere_embed":      "cohere_embed",
	"voyage_embed":      "voyage_ai",
	"jina_embed":        "jina_embeddings",
	"mixedbread_embed":  "mixedbread_embed",
	"nomic_embed":       "nomic_embed",
	// ── Translation ────────────────────────────────────────────────────
	"deepl_translate":   "deepl_translate",
	"translate_text":    "deepl_translate",
	"google_translate":  "google_translate",
	"aws_translate":     "aws_translate",
	"azure_translate":   "azure_translator",
	// ── Vision / OCR ───────────────────────────────────────────────────
	"google_vision_annotate":  "google_vision",
	"google_vision_ocr":       "google_vision",
	"azure_vision_analyze":    "azure_computer_vision",
	"azure_ocr":               "azure_computer_vision",
	"aws_rekognition_detect":  "aws_rekognition",
	"aws_rekognition_labels":  "aws_rekognition",
	"mathpix_process":         "mathpix_ocr",
	// ── Video AI ───────────────────────────────────────────────────────
	"runway_generate":      "runway_video",
	"heygen_create_video":  "heygen_video",
	"luma_generate":        "luma_video",
	"mux_upload":           "mux_video",
	"mux_create_asset":     "mux_video",
	// ── Messaging / Notification ───────────────────────────────────────
	"slack_post_message":    "slack_api",
	"slack_send_message":    "slack_api",
	"slack_chat_post":       "slack_api",
	"discord_send_message":  "discord_api",
	"discord_create_message": "discord_api",
	"telegram_send_message": "telegram_api",
	"telegram_send":         "telegram_api",
	"twilio_call":           "twilio_voice",
	"twilio_make_call":      "twilio_voice",
	"onesignal_send":        "onesignal",
	"pusher_trigger":        "pusher",
	"novu_trigger":          "novu",
	// ── Database ───────────────────────────────────────────────────────
	"neon_query":             "neon_postgres",
	"neon_sql":               "neon_postgres",
	"planetscale_query":      "planetscale",
	"turso_query":            "turso_db",
	"turso_execute":          "turso_db",
	"mongodb_find":           "mongodb_atlas",
	"mongodb_query":          "mongodb_atlas",
	"mongodb_insert":         "mongodb_atlas",
	"upstash_redis_get":      "upstash_redis",
	"upstash_redis_set":      "upstash_redis",
	"upstash_redis_command":  "upstash_redis",
	"fauna_query":            "fauna_db",
	"cockroach_query":        "cockroachdb",
	// ── Project Management ─────────────────────────────────────────────
	"jira_create_issue":    "jira_api",
	"jira_search":          "jira_api",
	"jira_update_issue":    "jira_api",
	"jira_get_issue":       "jira_api",
	"linear_create_issue":  "linear_api",
	"linear_search":        "linear_api",
	"linear_update_issue":  "linear_api",
	"asana_create_task":    "asana_api",
	"asana_search":         "asana_api",
	"clickup_create_task":  "clickup_api",
	"clickup_search":       "clickup_api",
	"notion_query":         "notion_api",
	"notion_create_page":   "notion_api",
	"notion_search":        "notion_api",
	"notion_update_page":   "notion_api",
	// ── CRM ────────────────────────────────────────────────────────────
	"salesforce_query":        "salesforce_api",
	"salesforce_create":       "salesforce_api",
	"salesforce_soql":         "salesforce_api",
	"hubspot_create_contact":  "hubspot_api",
	"hubspot_search":          "hubspot_api",
	"hubspot_get_contacts":    "hubspot_api",
	// ── DevOps / Code ──────────────────────────────────────────────────
	"github_create_issue":     "github_api",
	"github_search":           "github_api",
	"github_create_pr":        "github_api",
	"github_get_file":         "github_api",
	"github_list_repos":       "github_api",
	"gitlab_create_issue":     "gitlab_api",
	"gitlab_search":           "gitlab_api",
	"vercel_deploy":           "vercel_api",
	"vercel_list_deployments": "vercel_api",
	// ── Social Media ───────────────────────────────────────────────────
	"twitter_post":          "twitter_api",
	"twitter_search":        "twitter_api",
	"x_post_tweet":          "twitter_api",
	"linkedin_post":         "linkedin_api",
	"linkedin_share":        "linkedin_api",
	"reddit_submit":         "reddit_api",
	"reddit_search":         "reddit_api",
	"youtube_search":        "youtube_data_api",
	"youtube_list_videos":   "youtube_data_api",
	// ── Weather / News / Geo ───────────────────────────────────────────
	"openweathermap_current":  "openweathermap",
	"openweathermap_forecast": "openweathermap",
	"get_weather":             "openweathermap",
	"weather_forecast":        "weatherapi",
	"get_news":                "newsapi",
	"news_search":             "newsapi",
	"ip_lookup":               "ipinfo",
	"geolocate":               "maxmind_geoip",
	// ── Workflow Automation ─────────────────────────────────────────────
	"zapier_nla":        "zapier",
	"zapier_action":     "zapier",
	"make_webhook":      "make_integromat",
	"pipedream_trigger": "pipedream",
	// ── Payment / E-commerce ───────────────────────────────────────────
	"paypal_create_payment":    "paypal",
	"paypal_capture":           "paypal",
	"razorpay_create_order":    "razorpay",
	"razorpay_capture_payment": "razorpay",
	"shopify_create_product":   "shopify_api",
	"shopify_get_orders":       "shopify_api",
	// ── Scraping (new) ─────────────────────────────────────────────────
	"scraperapi_scrape": "scraperapi",
	"zenrows_scrape":    "zenrows",
	"oxylabs_scrape":    "oxylabs_scraper",
	// ── AI/ML Platforms ────────────────────────────────────────────────
	"huggingface_inference": "huggingface_inference",
	"hf_generate":          "huggingface_inference",
	"roboflow_detect":      "roboflow",
	"scale_create_task":    "scale_ai",
	"together_generate":    "together_ai",
	"groq_chat":            "groq_inference",
	"fireworks_generate":   "fireworks_ai",
	"cohere_rerank":        "cohere_rerank",
	"jina_rerank":          "jina_reranker",
	"rerank":               "cohere_rerank",
	// ── Cloud Storage (new) ────────────────────────────────────────────
	"gcs_upload":        "gcs",
	"gcs_download":      "gcs",
	"r2_put_object":     "r2_cloudflare",
	"r2_get_object":     "r2_cloudflare",
	"azure_blob_upload": "azure_blob",
	// ── Document / PDF (new) ───────────────────────────────────────────
	"adobe_pdf_extract": "adobe_pdf_services",
	"docraptor_convert": "docraptor",
	// ── Speech (new) ───────────────────────────────────────────────────
	"openai_tts":             "openai_tts",
	"openai_text_to_speech":  "openai_tts",
	"resemble_generate":      "resemble_ai",
	"playht_generate":        "playht",
	// ── Image (new) ────────────────────────────────────────────────────
	"midjourney_generate": "midjourney_api",
	"fal_generate":        "flux_fal",
	"flux_generate":       "flux_fal",
	"leonardo_generate":   "leonardo_ai",
	// ── Data Enrichment (new) ──────────────────────────────────────────
	"apollo_search":    "apollo_io",
	"apollo_enrich":    "apollo_io",
	"zoominfo_search":  "zoominfo_api",
	"zoominfo_enrich":  "zoominfo_api",
	// ── E-signature / Calendar ─────────────────────────────────────────
	"docusign_send":            "docusign",
	"docusign_create_envelope": "docusign",
	"calendly_create_event":    "calendly_api",
	// ── Monitoring ─────────────────────────────────────────────────────
	"sentry_capture":  "sentry",
	"datadog_submit":  "datadog",
	"posthog_capture": "posthog",
	"mixpanel_track":  "mixpanel",
	"segment_track":   "segment",
	// ── CDN / Media ────────────────────────────────────────────────────
	"cloudinary_upload":    "cloudinary",
	"cloudinary_transform": "cloudinary",
	"uploadthing_upload":   "uploadthing",
	// ── Compute (new) ──────────────────────────────────────────────────
	"fly_machine_run":        "fly_machines",
	"cloudflare_worker_run":  "cloudflare_workers",
	// ── Vector DB (new) ────────────────────────────────────────────────
	"chroma_query":       "chroma_cloud",
	"chroma_add":         "chroma_cloud",
	"turbopuffer_query":  "turbopuffer",
	"turbopuffer_upsert": "turbopuffer",
	// ── Agent Tools (new) ──────────────────────────────────────────────
	"browserless_navigate":   "browserless",
	"browserless_screenshot": "browserless",
	"browserless_scrape":     "browserless",
}

// MCPCallInfo carries per-call detail an MCP tool invocation might want to
// surface on the recorded Event (server name, latency, error flag).
type MCPCallInfo struct {
	ToolName  string
	Server    string // best-effort server identifier; pass "" if unknown
	LatencyMs int
	IsError   bool
}

// RecordMCPResponse records an external_cost event for an MCP tool call.
//
// Cost resolution mirrors the Python helper at instruments/mcp.py:348:
//  1. Rate registry: explicit "mcp:<tool_name>" rate.
//  2. Service catalog mapping: well-known tool -> catalog key -> rate registry.
//  3. Fallback: cost=0, confidence="unknown".
func RecordMCPResponse(
	buffer core.Buffer,
	rates *pricing.RateRegistry,
	taskID uuid.UUID,
	info MCPCallInfo,
) (core.Event, error) {
	if info.ToolName == "" {
		return core.Event{}, fmt.Errorf("clients: MCP tool_name is required")
	}

	cost, confidence, source, version := resolveMCPCost(rates, info.ToolName)

	server := info.Server
	if server == "" {
		server = "unknown"
	}

	event := core.NewEvent(taskID, core.EventTypeExternalCost)
	event.ServiceName = "mcp:" + info.ToolName
	event.CostUSD = cost
	event.CostConfidence = confidence
	event.PricingSource = source
	event.PricingVersion = version
	if info.LatencyMs > 0 {
		ms := info.LatencyMs
		event.LatencyMs = &ms
	}
	event.Details["mcp_tool"] = info.ToolName
	event.Details["mcp_server"] = server
	event.Details["latency_ms"] = info.LatencyMs
	event.Details["is_error"] = info.IsError

	if err := buffer.InsertEvent(event); err != nil {
		return core.Event{}, fmt.Errorf("clients: insert mcp event: %w", err)
	}
	return event, nil
}

// resolveMCPCost looks up the cost for an MCP tool call.
func resolveMCPCost(
	rates *pricing.RateRegistry,
	toolName string,
) (decimal.Decimal, core.CostConfidence, core.PricingSource, string) {
	if rates != nil {
		// 1. Direct MCP tool rate.
		if entry := rates.Get("mcp:" + toolName); entry != nil {
			return entry.CostUSD, core.CostConfidenceComputed, core.PricingSourceRateRegistry, rates.PricingVersion()
		}
		// 2. Catalog-keyed lookup via the well-known tool map.
		if catalogKey, ok := MCPToolMap[toolName]; ok {
			if entry := rates.Get(catalogKey); entry != nil {
				return entry.CostUSD, core.CostConfidenceComputed, core.PricingSourceRateRegistry, rates.PricingVersion()
			}
		}
	}

	return decimal.Zero, core.CostConfidenceUnknown, core.PricingSourceUnknown, ""
}
