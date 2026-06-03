/**
 * MCP (Model Context Protocol) auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `Client.prototype.callTool` to automatically record cost
 * events for every MCP tool invocation within an active task context.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask } from "../core/context.js";
import { createAutoTask } from "../core/auto-task.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _original: Function | null = null;
let _clientClass: any = null;
let _buffer: EventBuffer | null = null;

/** Test helper: inject a mock Client class so tests avoid importing @modelcontextprotocol/sdk. */
export function _setClientClass(cls: any): void {
  _clientClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetClientClass(): void {
  _clientClass = null;
}

// ---------------------------------------------------------------------------
// MCP tool name → service catalog key mapping
//
// Comprehensive mapping covering all 163 services in service_prices.json.
// ---------------------------------------------------------------------------

const MCP_TOOL_MAP: Record<string, string> = {
  // Search
  tavily_search: "tavily_search",
  tavily_extract: "tavily_search",
  tavily_crawl: "tavily_search",
  tavily_map: "tavily_search",
  tavily_research: "tavily_search",
  serper_search: "serper_search",
  serper_google_search: "serper_search",
  exa_search: "exa_search",
  exa_find_similar: "exa_search",
  exa_get_contents: "exa_search",
  perplexity_search: "perplexity_search",
  sonar_search: "perplexity_search",
  brave_web_search: "brave_search",
  brave_local_search: "brave_search",
  brave_search: "brave_search",
  bing_search: "bing_search",
  bing_web_search: "bing_search",
  serpapi_search: "serpapi",
  serpapi_google_search: "serpapi",
  // Scraping
  firecrawl_scrape: "firecrawl_scrape",
  firecrawl_crawl: "firecrawl_scrape",
  firecrawl_map: "firecrawl_scrape",
  firecrawl_search: "firecrawl_scrape",
  firecrawl_extract: "firecrawl_scrape",
  browserbase_create_session: "browserbase",
  browserbase_navigate: "browserbase",
  browserbase_screenshot: "browserbase",
  browserbase_get_content: "browserbase",
  scrapingbee_scrape: "scrapingbee",
  apify_run_actor: "apify",
  apify_get_dataset: "apify",
  scrapingdog_scrape: "scrapingdog",
  diffbot_analyze: "diffbot",
  diffbot_extract: "diffbot",
  jina_read: "jina_reader",
  jina_reader: "jina_reader",
  jina_search: "jina_reader",
  // Vector DBs
  pinecone_query: "pinecone_query",
  pinecone_upsert: "pinecone_query",
  pinecone_delete: "pinecone_query",
  pinecone_fetch: "pinecone_query",
  weaviate_query: "weaviate_cloud",
  weaviate_search: "weaviate_cloud",
  qdrant_search: "qdrant_cloud",
  qdrant_query: "qdrant_cloud",
  qdrant_upsert: "qdrant_cloud",
  milvus_search: "milvus_zilliz",
  milvus_query: "milvus_zilliz",
  astra_query: "astra_db",
  astra_search: "astra_db",
  // Compute / Sandbox
  e2b_run_code: "e2b_sandbox",
  e2b_execute: "e2b_sandbox",
  e2b_create_sandbox: "e2b_sandbox",
  code_execute: "e2b_sandbox",
  modal_run: "modal_compute",
  modal_deploy: "modal_compute",
  lambda_invoke: "aws_lambda",
  // Communication
  twilio_send_sms: "twilio_sms",
  twilio_send_message: "twilio_sms",
  sendgrid_send_email: "sendgrid_email",
  send_email: "sendgrid_email",
  resend_send_email: "resend_email",
  mailgun_send: "mailgun",
  postmark_send: "postmark",
  // Maps
  google_maps_geocode: "google_maps_geocode",
  google_maps_search: "google_maps_places",
  google_maps_directions: "google_maps_directions",
  google_maps_places: "google_maps_places",
  mapbox_geocode: "mapbox_geocoding",
  mapbox_directions: "mapbox_geocoding",
  // Data Enrichment
  people_search: "people_data_labs",
  pdl_search: "people_data_labs",
  pdl_enrich: "people_data_labs",
  clearbit_enrich: "clearbit_enrichment",
  clearbit_lookup: "clearbit_enrichment",
  hunter_search: "hunter_io",
  hunter_email_finder: "hunter_io",
  crunchbase_search: "crunchbase",
  crunchbase_lookup: "crunchbase",
  // Payments
  stripe_create_charge: "stripe_payment",
  stripe_create_payment: "stripe_payment",
  stripe_create_payment_link: "stripe_payment",
  stripe_list_charges: "stripe_payment",
  // Speech / Audio
  elevenlabs_tts: "eleven_labs",
  elevenlabs_text_to_speech: "eleven_labs",
  deepgram_transcribe: "deepgram_transcription",
  assemblyai_transcribe: "assemblyai",
  whisper_transcribe: "openai_whisper",
  // Image Generation
  dalle_generate: "openai_dalle",
  openai_generate_image: "openai_dalle",
  stability_generate: "stability_ai",
  replicate_run: "replicate",
  replicate_predict: "replicate",
  // Document Processing
  textract_analyze: "amazon_textract",
  textract_detect: "amazon_textract",
  document_ai_process: "google_document_ai",
  unstructured_parse: "unstructured_io",
  llamaparse_parse: "llamaparse",
  llamaparse_upload: "llamaparse",
  // Financial Data
  twelve_data_quote: "twelve_data",
  twelve_data_time_series: "twelve_data",
  alpha_vantage_quote: "alpha_vantage",
  polygon_quote: "polygon_io",
  polygon_aggs: "polygon_io",
  coinapi_exchange_rate: "coinapi",
  // Cloud Storage
  s3_get_object: "aws_s3",
  s3_put_object: "aws_s3",
  supabase_query: "supabase",
  supabase_insert: "supabase",
  supabase_select: "supabase",
  // Agent Tools
  composio_execute: "composio",
  composio_action: "composio",
  wolfram_query: "wolfram_alpha",
  wolfram_alpha_query: "wolfram_alpha",
  // Embeddings
  openai_embed: "openai_embeddings",
  cohere_embed: "cohere_embed",
  voyage_embed: "voyage_ai",
  jina_embed: "jina_embeddings",
  mixedbread_embed: "mixedbread_embed",
  nomic_embed: "nomic_embed",
  // Translation
  deepl_translate: "deepl_translate",
  translate_text: "deepl_translate",
  google_translate: "google_translate",
  aws_translate: "aws_translate",
  azure_translate: "azure_translator",
  // Vision / OCR
  google_vision_annotate: "google_vision",
  google_vision_ocr: "google_vision",
  azure_vision_analyze: "azure_computer_vision",
  azure_ocr: "azure_computer_vision",
  aws_rekognition_detect: "aws_rekognition",
  aws_rekognition_labels: "aws_rekognition",
  mathpix_process: "mathpix_ocr",
  // Video AI
  runway_generate: "runway_video",
  heygen_create_video: "heygen_video",
  luma_generate: "luma_video",
  mux_upload: "mux_video",
  mux_create_asset: "mux_video",
  // Messaging / Notification
  slack_post_message: "slack_api",
  slack_send_message: "slack_api",
  slack_chat_post: "slack_api",
  discord_send_message: "discord_api",
  discord_create_message: "discord_api",
  telegram_send_message: "telegram_api",
  telegram_send: "telegram_api",
  twilio_call: "twilio_voice",
  twilio_make_call: "twilio_voice",
  onesignal_send: "onesignal",
  pusher_trigger: "pusher",
  novu_trigger: "novu",
  // Database
  neon_query: "neon_postgres",
  neon_sql: "neon_postgres",
  planetscale_query: "planetscale",
  turso_query: "turso_db",
  turso_execute: "turso_db",
  mongodb_find: "mongodb_atlas",
  mongodb_query: "mongodb_atlas",
  mongodb_insert: "mongodb_atlas",
  upstash_redis_get: "upstash_redis",
  upstash_redis_set: "upstash_redis",
  upstash_redis_command: "upstash_redis",
  fauna_query: "fauna_db",
  cockroach_query: "cockroachdb",
  // Project Management
  jira_create_issue: "jira_api",
  jira_search: "jira_api",
  jira_update_issue: "jira_api",
  jira_get_issue: "jira_api",
  linear_create_issue: "linear_api",
  linear_search: "linear_api",
  linear_update_issue: "linear_api",
  asana_create_task: "asana_api",
  asana_search: "asana_api",
  clickup_create_task: "clickup_api",
  clickup_search: "clickup_api",
  notion_query: "notion_api",
  notion_create_page: "notion_api",
  notion_search: "notion_api",
  notion_update_page: "notion_api",
  // CRM
  salesforce_query: "salesforce_api",
  salesforce_create: "salesforce_api",
  salesforce_soql: "salesforce_api",
  hubspot_create_contact: "hubspot_api",
  hubspot_search: "hubspot_api",
  hubspot_get_contacts: "hubspot_api",
  // DevOps / Code
  github_create_issue: "github_api",
  github_search: "github_api",
  github_create_pr: "github_api",
  github_get_file: "github_api",
  github_list_repos: "github_api",
  gitlab_create_issue: "gitlab_api",
  gitlab_search: "gitlab_api",
  vercel_deploy: "vercel_api",
  vercel_list_deployments: "vercel_api",
  // Social Media
  twitter_post: "twitter_api",
  twitter_search: "twitter_api",
  x_post_tweet: "twitter_api",
  linkedin_post: "linkedin_api",
  linkedin_share: "linkedin_api",
  reddit_submit: "reddit_api",
  reddit_search: "reddit_api",
  youtube_search: "youtube_data_api",
  youtube_list_videos: "youtube_data_api",
  // Weather / News / Geo
  openweathermap_current: "openweathermap",
  openweathermap_forecast: "openweathermap",
  get_weather: "openweathermap",
  weather_forecast: "weatherapi",
  get_news: "newsapi",
  news_search: "newsapi",
  ip_lookup: "ipinfo",
  geolocate: "maxmind_geoip",
  // Workflow Automation
  zapier_nla: "zapier",
  zapier_action: "zapier",
  make_webhook: "make_integromat",
  pipedream_trigger: "pipedream",
  // Payment / E-commerce
  paypal_create_payment: "paypal",
  paypal_capture: "paypal",
  razorpay_create_order: "razorpay",
  razorpay_capture_payment: "razorpay",
  shopify_create_product: "shopify_api",
  shopify_get_orders: "shopify_api",
  // Scraping (new)
  scraperapi_scrape: "scraperapi",
  zenrows_scrape: "zenrows",
  oxylabs_scrape: "oxylabs_scraper",
  // AI/ML Platforms
  huggingface_inference: "huggingface_inference",
  hf_generate: "huggingface_inference",
  roboflow_detect: "roboflow",
  scale_create_task: "scale_ai",
  together_generate: "together_ai",
  groq_chat: "groq_inference",
  fireworks_generate: "fireworks_ai",
  cohere_rerank: "cohere_rerank",
  jina_rerank: "jina_reranker",
  rerank: "cohere_rerank",
  // Cloud Storage (new)
  gcs_upload: "gcs",
  gcs_download: "gcs",
  r2_put_object: "r2_cloudflare",
  r2_get_object: "r2_cloudflare",
  azure_blob_upload: "azure_blob",
  // Document (new)
  adobe_pdf_extract: "adobe_pdf_services",
  docraptor_convert: "docraptor",
  // Speech (new)
  openai_tts: "openai_tts",
  openai_text_to_speech: "openai_tts",
  resemble_generate: "resemble_ai",
  playht_generate: "playht",
  // Image (new)
  midjourney_generate: "midjourney_api",
  fal_generate: "flux_fal",
  flux_generate: "flux_fal",
  leonardo_generate: "leonardo_ai",
  // Data Enrichment (new)
  apollo_search: "apollo_io",
  apollo_enrich: "apollo_io",
  zoominfo_search: "zoominfo_api",
  // E-signature / Calendar
  docusign_send: "docusign",
  docusign_create_envelope: "docusign",
  calendly_create_event: "calendly_api",
  // Monitoring
  sentry_capture: "sentry",
  datadog_submit: "datadog",
  posthog_capture: "posthog",
  mixpanel_track: "mixpanel",
  segment_track: "segment",
  // CDN / Media
  cloudinary_upload: "cloudinary",
  cloudinary_transform: "cloudinary",
  // Compute (new)
  fly_machine_run: "fly_machines",
  cloudflare_worker_run: "cloudflare_workers",
  // Vector DB (new)
  chroma_query: "chroma_cloud",
  chroma_add: "chroma_cloud",
  turbopuffer_query: "turbopuffer",
  turbopuffer_upsert: "turbopuffer",
  // Agent Tools (new)
  browserless_navigate: "browserless",
  browserless_screenshot: "browserless",
  browserless_scrape: "browserless",
};

// ---------------------------------------------------------------------------
// Instrument functions
// ---------------------------------------------------------------------------

/**
 * Patch `Client.prototype.callTool` to record cost events.
 *
 * If `@modelcontextprotocol/sdk` is not installed and no mock class is
 * injected, the dynamic import will throw and the function will reject.
 */
export async function instrumentMcp(
  _pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let ClientProto: any;
  if (_clientClass) {
    ClientProto = _clientClass.prototype;
  } else {
    // @ts-expect-error -- @modelcontextprotocol/sdk types are not bundled
    const mcpSdk = await import("@modelcontextprotocol/sdk/client/index.js");
    const Client = mcpSdk.Client;
    ClientProto = Client.prototype;
  }

  _original = ClientProto.callTool;
  _buffer = buffer;

  ClientProto.callTool = async function (
    this: any,
    params: any,
    ...rest: any[]
  ): Promise<any> {
    let task = getCurrentTask();

    if (!task) {
      task = createAutoTask("mcp.tool_call");
      _buffer?.upsertTask(task);
    }

    const toolName: string = params?.name ?? "unknown";
    const startTime = performance.now();
    let isError = false;

    try {
      const result = await _original!.call(this, params, ...rest);
      const latencyMs = Math.round(performance.now() - startTime);

      // Check if MCP result itself signals an error
      if (result?.isError) {
        isError = true;
      }

      try {
        recordMcpEvent(toolName, task, latencyMs, isError, this);
      } catch {
        // dexcost errors must never crash user code
      }
      return result;
    } catch (err) {
      isError = true;
      const latencyMs = Math.round(performance.now() - startTime);
      try {
        recordMcpEvent(toolName, task, latencyMs, isError, this);
      } catch {
        // dexcost errors must never crash user code
      }
      throw err;
    }
  };

  _patched = true;
}

/**
 * Remove the monkey-patch and restore the original `callTool` method.
 */
export function uninstrumentMcp(): void {
  if (!_patched || !_original) return;

  if (_clientClass) {
    _clientClass.prototype.callTool = _original;
  }

  _original = null;
  _buffer = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordMcpEvent(
  toolName: string,
  task: Task,
  latencyMs: number,
  isError: boolean,
  instance: any,
): void {
  if (!_buffer) return;

  const { costUsd, costConfidence, pricingSource } = resolveCost(toolName);
  const mcpServer: string = instance?._serverName ?? "unknown";

  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "external_cost",
    costUsd,
    costConfidence,
    pricingSource,
    serviceName: `mcp:${toolName}`,
    latencyMs,
    isRetry: false,
    details: {
      mcp_tool: toolName,
      mcp_server: mcpServer,
      latency_ms: latencyMs,
      is_error: isError,
    },
  });

  _buffer.addEvent(event);

  task.externalCostUsd = task.externalCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  _buffer.upsertTask(task);
}

function resolveCost(toolName: string): {
  costUsd: number;
  costConfidence: CostConfidence;
  pricingSource: PricingSource;
} {
  // Service catalog mapping — recognised tools get "estimated" confidence
  // since we know the service but not exact usage-based cost from this call.
  const catalogKey = MCP_TOOL_MAP[toolName];
  if (catalogKey) {
    return { costUsd: 0, costConfidence: "estimated", pricingSource: "service_catalog" };
  }

  // Unknown tool
  return { costUsd: 0, costConfidence: "unknown", pricingSource: "unknown" };
}

// Self-register so the instrument registry can discover us.
registerInstrument("mcp", instrumentMcp, uninstrumentMcp);
