"""Auto-instrumentation for MCP (Model Context Protocol) tool calls.

Monkey-patches ``mcp.ClientSession.call_tool`` using :pypi:`wrapt` so that
every MCP tool invocation inside an active :class:`~dexcost.tracker.CostTracker`
task is automatically recorded as an ``external_cost`` event.

Usage::

    from dexcost import CostTracker, instrument_mcp

    tracker = CostTracker()
    instrument_mcp(tracker)

    # All subsequent MCP call_tool() invocations inside a
    # tracked task are captured automatically.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.context import _current_task, get_current_task, set_current_task, suppress_network_event
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# MCP tool name -> service catalog key mapping
#
# Comprehensive mapping covering all 163 services in service_prices.json.
# MCP tool names vary by server implementation, so we map common variants
# (official MCP server names, community aliases, snake_case/camelCase).
# ---------------------------------------------------------------------------

_MCP_TOOL_MAP: dict[str, str] = {
    # ── Search ──────────────────────────────────────────────────────────
    "tavily_search": "tavily_search",
    "tavily_extract": "tavily_search",
    "tavily_crawl": "tavily_search",
    "tavily_map": "tavily_search",
    "tavily_research": "tavily_search",
    "serper_search": "serper_search",
    "serper_google_search": "serper_search",
    "exa_search": "exa_search",
    "exa_find_similar": "exa_search",
    "exa_get_contents": "exa_search",
    "perplexity_search": "perplexity_search",
    "sonar_search": "perplexity_search",
    "brave_web_search": "brave_search",
    "brave_local_search": "brave_search",
    "brave_search": "brave_search",
    "bing_search": "bing_search",
    "bing_web_search": "bing_search",
    "serpapi_search": "serpapi",
    "serpapi_google_search": "serpapi",
    # ── Scraping ────────────────────────────────────────────────────────
    "firecrawl_scrape": "firecrawl_scrape",
    "firecrawl_crawl": "firecrawl_scrape",
    "firecrawl_map": "firecrawl_scrape",
    "firecrawl_search": "firecrawl_scrape",
    "firecrawl_extract": "firecrawl_scrape",
    "browserbase_create_session": "browserbase",
    "browserbase_navigate": "browserbase",
    "browserbase_screenshot": "browserbase",
    "browserbase_get_content": "browserbase",
    "scrapingbee_scrape": "scrapingbee",
    "apify_run_actor": "apify",
    "apify_get_dataset": "apify",
    "scrapingdog_scrape": "scrapingdog",
    "diffbot_analyze": "diffbot",
    "diffbot_extract": "diffbot",
    "jina_read": "jina_reader",
    "jina_reader": "jina_reader",
    "jina_search": "jina_reader",
    # ── Vector Databases ────────────────────────────────────────────────
    "pinecone_query": "pinecone_query",
    "pinecone_upsert": "pinecone_query",
    "pinecone_delete": "pinecone_query",
    "pinecone_fetch": "pinecone_query",
    "weaviate_query": "weaviate_cloud",
    "weaviate_search": "weaviate_cloud",
    "qdrant_search": "qdrant_cloud",
    "qdrant_query": "qdrant_cloud",
    "qdrant_upsert": "qdrant_cloud",
    "milvus_search": "milvus_zilliz",
    "milvus_query": "milvus_zilliz",
    "astra_query": "astra_db",
    "astra_search": "astra_db",
    # ── Compute / Sandbox ───────────────────────────────────────────────
    "e2b_run_code": "e2b_sandbox",
    "e2b_execute": "e2b_sandbox",
    "e2b_create_sandbox": "e2b_sandbox",
    "code_execute": "e2b_sandbox",
    "modal_run": "modal_compute",
    "modal_deploy": "modal_compute",
    "lambda_invoke": "aws_lambda",
    # ── Communication ───────────────────────────────────────────────────
    "twilio_send_sms": "twilio_sms",
    "twilio_send_message": "twilio_sms",
    "sendgrid_send_email": "sendgrid_email",
    "send_email": "sendgrid_email",
    "resend_send_email": "resend_email",
    "mailgun_send": "mailgun",
    "postmark_send": "postmark",
    # ── Maps / Geocoding ────────────────────────────────────────────────
    "google_maps_geocode": "google_maps_geocode",
    "google_maps_search": "google_maps_places",
    "google_maps_directions": "google_maps_directions",
    "google_maps_places": "google_maps_places",
    "mapbox_geocode": "mapbox_geocoding",
    "mapbox_directions": "mapbox_geocoding",
    # ── Data Enrichment ─────────────────────────────────────────────────
    "people_search": "people_data_labs",
    "pdl_search": "people_data_labs",
    "pdl_enrich": "people_data_labs",
    "clearbit_enrich": "clearbit_enrichment",
    "clearbit_lookup": "clearbit_enrichment",
    "hunter_search": "hunter_io",
    "hunter_email_finder": "hunter_io",
    "crunchbase_search": "crunchbase",
    "crunchbase_lookup": "crunchbase",
    # ── Payments ────────────────────────────────────────────────────────
    "stripe_create_charge": "stripe_payment",
    "stripe_create_payment": "stripe_payment",
    "stripe_create_payment_link": "stripe_payment",
    "stripe_list_charges": "stripe_payment",
    # ── Speech / Audio ──────────────────────────────────────────────────
    "elevenlabs_tts": "eleven_labs",
    "elevenlabs_text_to_speech": "eleven_labs",
    "deepgram_transcribe": "deepgram_transcription",
    "assemblyai_transcribe": "assemblyai",
    "whisper_transcribe": "openai_whisper",
    # ── Image Generation ────────────────────────────────────────────────
    "dalle_generate": "openai_dalle",
    "openai_generate_image": "openai_dalle",
    "stability_generate": "stability_ai",
    "replicate_run": "replicate",
    "replicate_predict": "replicate",
    # ── Document Processing ─────────────────────────────────────────────
    "textract_analyze": "amazon_textract",
    "textract_detect": "amazon_textract",
    "document_ai_process": "google_document_ai",
    "unstructured_parse": "unstructured_io",
    "llamaparse_parse": "llamaparse",
    "llamaparse_upload": "llamaparse",
    # ── Financial Data ──────────────────────────────────────────────────
    "twelve_data_quote": "twelve_data",
    "twelve_data_time_series": "twelve_data",
    "alpha_vantage_quote": "alpha_vantage",
    "polygon_quote": "polygon_io",
    "polygon_aggs": "polygon_io",
    "coinapi_exchange_rate": "coinapi",
    # ── Cloud Storage ───────────────────────────────────────────────────
    "s3_get_object": "aws_s3",
    "s3_put_object": "aws_s3",
    "supabase_query": "supabase",
    "supabase_insert": "supabase",
    "supabase_select": "supabase",
    # ── Agent Tools ─────────────────────────────────────────────────────
    "composio_execute": "composio",
    "composio_action": "composio",
    "wolfram_query": "wolfram_alpha",
    "wolfram_alpha_query": "wolfram_alpha",
    # ── Embeddings ──────────────────────────────────────────────────────
    "openai_embed": "openai_embeddings",
    "cohere_embed": "cohere_embed",
    "voyage_embed": "voyage_ai",
    "jina_embed": "jina_embeddings",
    "mixedbread_embed": "mixedbread_embed",
    "nomic_embed": "nomic_embed",
    # ── Translation ────────────────────────────────────────────────────
    "deepl_translate": "deepl_translate",
    "translate_text": "deepl_translate",
    "google_translate": "google_translate",
    "aws_translate": "aws_translate",
    "azure_translate": "azure_translator",
    # ── Vision / OCR ───────────────────────────────────────────────────
    "google_vision_annotate": "google_vision",
    "google_vision_ocr": "google_vision",
    "azure_vision_analyze": "azure_computer_vision",
    "azure_ocr": "azure_computer_vision",
    "aws_rekognition_detect": "aws_rekognition",
    "aws_rekognition_labels": "aws_rekognition",
    "mathpix_process": "mathpix_ocr",
    # ── Video AI ───────────────────────────────────────────────────────
    "runway_generate": "runway_video",
    "heygen_create_video": "heygen_video",
    "luma_generate": "luma_video",
    "mux_upload": "mux_video",
    "mux_create_asset": "mux_video",
    # ── Messaging / Notification ───────────────────────────────────────
    "slack_post_message": "slack_api",
    "slack_send_message": "slack_api",
    "slack_chat_post": "slack_api",
    "discord_send_message": "discord_api",
    "discord_create_message": "discord_api",
    "telegram_send_message": "telegram_api",
    "telegram_send": "telegram_api",
    "twilio_call": "twilio_voice",
    "twilio_make_call": "twilio_voice",
    "onesignal_send": "onesignal",
    "pusher_trigger": "pusher",
    "novu_trigger": "novu",
    # ── Database ───────────────────────────────────────────────────────
    "neon_query": "neon_postgres",
    "neon_sql": "neon_postgres",
    "planetscale_query": "planetscale",
    "turso_query": "turso_db",
    "turso_execute": "turso_db",
    "mongodb_find": "mongodb_atlas",
    "mongodb_query": "mongodb_atlas",
    "mongodb_insert": "mongodb_atlas",
    "upstash_redis_get": "upstash_redis",
    "upstash_redis_set": "upstash_redis",
    "upstash_redis_command": "upstash_redis",
    "fauna_query": "fauna_db",
    "cockroach_query": "cockroachdb",
    # ── Project Management ─────────────────────────────────────────────
    "jira_create_issue": "jira_api",
    "jira_search": "jira_api",
    "jira_update_issue": "jira_api",
    "jira_get_issue": "jira_api",
    "linear_create_issue": "linear_api",
    "linear_search": "linear_api",
    "linear_update_issue": "linear_api",
    "asana_create_task": "asana_api",
    "asana_search": "asana_api",
    "clickup_create_task": "clickup_api",
    "clickup_search": "clickup_api",
    "notion_query": "notion_api",
    "notion_create_page": "notion_api",
    "notion_search": "notion_api",
    "notion_update_page": "notion_api",
    # ── CRM ────────────────────────────────────────────────────────────
    "salesforce_query": "salesforce_api",
    "salesforce_create": "salesforce_api",
    "salesforce_soql": "salesforce_api",
    "hubspot_create_contact": "hubspot_api",
    "hubspot_search": "hubspot_api",
    "hubspot_get_contacts": "hubspot_api",
    # ── DevOps / Code ──────────────────────────────────────────────────
    "github_create_issue": "github_api",
    "github_search": "github_api",
    "github_create_pr": "github_api",
    "github_get_file": "github_api",
    "github_list_repos": "github_api",
    "gitlab_create_issue": "gitlab_api",
    "gitlab_search": "gitlab_api",
    "vercel_deploy": "vercel_api",
    "vercel_list_deployments": "vercel_api",
    # ── Social Media ───────────────────────────────────────────────────
    "twitter_post": "twitter_api",
    "twitter_search": "twitter_api",
    "x_post_tweet": "twitter_api",
    "linkedin_post": "linkedin_api",
    "linkedin_share": "linkedin_api",
    "reddit_submit": "reddit_api",
    "reddit_search": "reddit_api",
    "youtube_search": "youtube_data_api",
    "youtube_list_videos": "youtube_data_api",
    # ── Weather / News / Geo ───────────────────────────────────────────
    "openweathermap_current": "openweathermap",
    "openweathermap_forecast": "openweathermap",
    "get_weather": "openweathermap",
    "weather_forecast": "weatherapi",
    "get_news": "newsapi",
    "news_search": "newsapi",
    "ip_lookup": "ipinfo",
    "geolocate": "maxmind_geoip",
    # ── Workflow Automation ─────────────────────────────────────────────
    "zapier_nla": "zapier",
    "zapier_action": "zapier",
    "make_webhook": "make_integromat",
    "pipedream_trigger": "pipedream",
    # ── Payment / E-commerce ───────────────────────────────────────────
    "paypal_create_payment": "paypal",
    "paypal_capture": "paypal",
    "razorpay_create_order": "razorpay",
    "razorpay_capture_payment": "razorpay",
    "shopify_create_product": "shopify_api",
    "shopify_get_orders": "shopify_api",
    # ── Scraping (new services) ────────────────────────────────────────
    "scraperapi_scrape": "scraperapi",
    "zenrows_scrape": "zenrows",
    "oxylabs_scrape": "oxylabs_scraper",
    # ── AI/ML Platforms ────────────────────────────────────────────────
    "huggingface_inference": "huggingface_inference",
    "hf_generate": "huggingface_inference",
    "roboflow_detect": "roboflow",
    "roboflow_classify": "roboflow",
    "scale_create_task": "scale_ai",
    "together_generate": "together_ai",
    "groq_chat": "groq_inference",
    "fireworks_generate": "fireworks_ai",
    "cohere_rerank": "cohere_rerank",
    "jina_rerank": "jina_reranker",
    # ── Reranking ──────────────────────────────────────────────────────
    "rerank": "cohere_rerank",
    # ── Authentication ─────────────────────────────────────────────────
    "auth0_get_user": "auth0",
    "clerk_get_user": "clerk_auth",
    # ── Cloud Storage ──────────────────────────────────────────────────
    "gcs_upload": "gcs",
    "gcs_download": "gcs",
    "r2_put_object": "r2_cloudflare",
    "r2_get_object": "r2_cloudflare",
    "azure_blob_upload": "azure_blob",
    # ── Document / PDF ─────────────────────────────────────────────────
    "adobe_pdf_extract": "adobe_pdf_services",
    "docraptor_convert": "docraptor",
    # ── Speech (new services) ──────────────────────────────────────────
    "openai_tts": "openai_tts",
    "openai_text_to_speech": "openai_tts",
    "resemble_generate": "resemble_ai",
    "playht_generate": "playht",
    # ── Image (new services) ───────────────────────────────────────────
    "midjourney_generate": "midjourney_api",
    "fal_generate": "flux_fal",
    "flux_generate": "flux_fal",
    "leonardo_generate": "leonardo_ai",
    # ── Data Enrichment (new) ──────────────────────────────────────────
    "apollo_search": "apollo_io",
    "apollo_enrich": "apollo_io",
    "zoominfo_search": "zoominfo_api",
    "zoominfo_enrich": "zoominfo_api",
    # ── E-signature / Calendar ─────────────────────────────────────────
    "docusign_send": "docusign",
    "docusign_create_envelope": "docusign",
    "calendly_create_event": "calendly_api",
    # ── Monitoring ─────────────────────────────────────────────────────
    "sentry_capture": "sentry",
    "datadog_submit": "datadog",
    "posthog_capture": "posthog",
    "mixpanel_track": "mixpanel",
    "segment_track": "segment",
    # ── CDN / Media ────────────────────────────────────────────────────
    "cloudinary_upload": "cloudinary",
    "cloudinary_transform": "cloudinary",
    "uploadthing_upload": "uploadthing",
    # ── Compute (new) ──────────────────────────────────────────────────
    "fly_machine_run": "fly_machines",
    "cloudflare_worker_run": "cloudflare_workers",
    # ── Vector DB (new) ────────────────────────────────────────────────
    "chroma_query": "chroma_cloud",
    "chroma_add": "chroma_cloud",
    "turbopuffer_query": "turbopuffer",
    "turbopuffer_upsert": "turbopuffer",
    # ── Browserless ────────────────────────────────────────────────────
    "browserless_navigate": "browserless",
    "browserless_screenshot": "browserless",
    "browserless_scrape": "browserless",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_mcp(tracker: Any) -> None:
    """Monkey-patch the MCP SDK to capture tool calls automatically.

    Patches ``mcp.client.session.ClientSession.call_tool`` (async).

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            look up rates and persist events.

    Raises:
        ImportError: If the ``mcp`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "MCP instrumentation is already active. "
            "Call uninstrument_mcp() before re-instrumenting."
        )

    try:
        import mcp.client.session as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required for MCP auto-instrumentation. "
            "Install it with: pip install mcp"
        ) from exc

    _active_tracker = tracker

    from mcp.client.session import ClientSession

    _originals["call_tool"] = ClientSession.call_tool

    wrapt.wrap_function_wrapper(
        "mcp.client.session",
        "ClientSession.call_tool",
        _call_tool_wrapper,
    )

    _patched = True


def uninstrument_mcp() -> None:
    """Remove MCP monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    from mcp.client.session import ClientSession

    if "call_tool" in _originals:
        ClientSession.call_tool = _originals["call_tool"]

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper function
# ---------------------------------------------------------------------------


def _call_tool_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``ClientSession.call_tool`` (async)."""
    # call_tool is always async in the MCP SDK, return the coroutine.
    return _async_call_tool_handler(wrapped, instance, args, kwargs)


async def _async_call_tool_handler(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """Async handler that records an MCP tool call as an external_cost event."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("mcp.tool_call")
        auto_token = set_current_task(auto_task_obj)

    try:
        # Extract tool name — call_tool(name: str, arguments: dict | None = None)
        tool_name: str = "unknown"
        if args:
            tool_name = str(args[0])
        elif "name" in kwargs:
            tool_name = str(kwargs["name"])

        start_time = time.perf_counter()
        is_error = False
        event: Event | None = None

        try:
            with suppress_network_event():
                result = await wrapped(*args, **kwargs)
        except Exception:
            is_error = True
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            try:
                event = _record_tool_call(
                    tool_name=tool_name,
                    instance=instance,
                    latency_ms=latency_ms,
                    is_error=True,
                )
            except Exception:
                _log.debug("dexcost: failed to record MCP error event", exc_info=True)
            raise

        latency_ms = int((time.perf_counter() - start_time) * 1000)

        # Check if the MCP result itself signals an error
        if hasattr(result, "isError") and result.isError:
            is_error = True

        try:
            event = _record_tool_call(
                tool_name=tool_name,
                instance=instance,
                latency_ms=latency_ms,
                is_error=is_error,
            )
        except Exception:
            _log.debug("dexcost: failed to record MCP event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize MCP auto-task", exc_info=True)

        return result
    except Exception:
        if auto and auto_task_obj is not None:
            _log.debug("dexcost: MCP auto-task call failed", exc_info=True)
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Cost resolution
# ---------------------------------------------------------------------------


def _resolve_cost(
    tool_name: str,
) -> tuple[Decimal, str, str, str | None]:
    """Resolve cost for an MCP tool call.

    Resolution order:
    1. Rate registry: ``"mcp:<tool_name>"``
    2. Service catalog mapping: tool_name -> catalog key -> rate registry
    3. Fallback: cost=0, confidence="unknown"

    Returns:
        ``(cost_usd, cost_confidence, pricing_source, pricing_version)``
    """
    tracker = _active_tracker
    if tracker is None:
        return (Decimal("0"), "unknown", "unknown", None)

    # 1. Rate registry: explicit MCP tool rate
    mcp_key = f"mcp:{tool_name}"
    rate = tracker.get_rate(mcp_key)
    if rate is not None:
        return (rate, "computed", "rate_registry", tracker.rate_registry.pricing_version)

    # 2. Service catalog mapping: well-known MCP tool -> catalog key
    catalog_key = _MCP_TOOL_MAP.get(tool_name)
    if catalog_key is not None:
        rate = tracker.get_rate(catalog_key)
        if rate is not None:
            return (rate, "computed", "rate_registry", tracker.rate_registry.pricing_version)

    # 3. Unknown tool or no rate registered
    return (Decimal("0"), "unknown", "unknown", None)


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


def _record_tool_call(
    *,
    tool_name: str,
    instance: Any,
    latency_ms: int,
    is_error: bool,
) -> Event | None:
    """Create and persist an external_cost event for an MCP tool call."""
    tracker = _active_tracker
    if tracker is None:
        return None
    task = get_current_task()
    if task is None:
        return None

    cost_usd, cost_confidence, pricing_source, pricing_version = _resolve_cost(tool_name)

    # Best-effort extraction of MCP server info
    mcp_server: str = getattr(instance, "_server_name", None) or "unknown"

    event = Event(
        task_id=task.task_id,
        event_type="external_cost",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name=f"mcp:{tool_name}",
        latency_ms=latency_ms,
        details={
            "mcp_tool": tool_name,
            "mcp_server": mcp_server,
            "latency_ms": latency_ms,
            "is_error": is_error,
        },
    )
    tracker._storage.insert_event(event)
    return event
