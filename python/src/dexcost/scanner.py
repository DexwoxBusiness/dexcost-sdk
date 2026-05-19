"""Code scanner for cost point detection (US-019).

Pure static analysis using stdlib ``ast`` — no API key needed, runs offline.

Detection layers
~~~~~~~~~~~~~~~~
1. **Import collection** — gather every imported module root + aliases.
2. **Assignment tracking** — follow ``client = openai.OpenAI()`` so that
   ``client.chat.completions.create(...)`` is recognised even though the
   resolved call string starts with ``client``, not ``openai``.
3. **Pattern matching** — match resolved call strings against known LLM,
   HTTP, service, and framework patterns.
"""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class CostPoint:
    """A detected cost point in source code."""

    file: str
    line: int
    category: str  # "llm" | "http" | "aws" | "vector_db" | "messaging" | ...
    auto_instrumented: bool
    description: str
    import_name: str  # e.g. "openai", "requests", "boto3"


@dataclass
class ScanResult:
    """Aggregated scan results."""

    cost_points: list[CostPoint] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def auto_count(self) -> int:
        return sum(1 for cp in self.cost_points if cp.auto_instrumented)

    @property
    def manual_count(self) -> int:
        return sum(1 for cp in self.cost_points if not cp.auto_instrumented)


# ── LLM provider patterns ────────────────────────────────────────────
# (import_root, call_pattern_substring, auto_instrumented, description)

_LLM_PATTERNS: list[tuple[str, str, bool, str]] = [
    # --- Auto-instrumented by dexcost ---
    ("openai", "completions.create", True, "OpenAI chat completion"),
    ("openai", "completions.acreate", True, "OpenAI async chat completion"),
    ("anthropic", "messages.create", True, "Anthropic message"),
    ("litellm", "completion", True, "LiteLLM completion"),
    ("litellm", "acompletion", True, "LiteLLM async completion"),

    # --- Additional LLM providers (need record_cost) ---
    # Groq — uses OpenAI-compatible SDK pattern
    ("groq", "completions.create", False, "Groq chat completion"),
    # Mistral — client.chat.complete()
    ("mistralai", "chat.complete", False, "Mistral chat completion"),
    ("mistralai", "chat.complete_async", False, "Mistral async chat completion"),
    ("mistralai", "chat.stream", False, "Mistral chat stream"),
    # DeepSeek — OpenAI-compatible client
    ("deepseek", "completions.create", False, "DeepSeek chat completion"),
    # Together AI
    ("together", "completions.create", False, "Together AI chat completion"),
    ("together", "chat.completions", False, "Together AI chat completion"),
    # Replicate
    ("replicate", "run", False, "Replicate model run"),
    ("replicate", "predictions.create", False, "Replicate prediction"),
    # Fireworks AI
    ("fireworks", "completions.create", False, "Fireworks AI completion"),
    # Cohere
    ("cohere", "chat", True, "Cohere chat"),
    ("cohere", "generate", True, "Cohere generate"),
    # Google
    ("google.generativeai", "generate_content", True, "Google Gemini"),
    ("google.genai", "generate_content", True, "Google Gemini"),
    ("genai", "generate_content", True, "Google Gemini"),
    ("vertexai", "generate_content", False, "Vertex AI generation"),
    # AWS Bedrock
    ("bedrock", "invoke_model", True, "AWS Bedrock invoke"),
    # Hugging Face
    ("huggingface_hub", "text_generation", False, "HuggingFace text generation"),
    ("huggingface_hub", "chat_completion", False, "HuggingFace chat completion"),
    # Ollama
    ("ollama", "chat", False, "Ollama chat"),
    ("ollama", "generate", False, "Ollama generate"),
]

# ── HTTP client patterns ──────────────────────────────────────────────
# (import_root, method_name, auto_instrumented, description)
# Matched if: root in imports AND method in call_str
# Also matched via assignment tracking for Session/Client instances.
#
# requests and httpx are auto-instrumented via Session.send / Client.send
# patches + domain matching against the 163-service catalog. aiohttp and
# urllib3 are NOT patched.

_HTTP_PATTERNS: list[tuple[str, str, bool, str]] = [
    # requests — auto-instrumented (Session.send patched by dexcost)
    ("requests", "get", True, "HTTP GET (requests)"),
    ("requests", "post", True, "HTTP POST (requests)"),
    ("requests", "put", True, "HTTP PUT (requests)"),
    ("requests", "delete", True, "HTTP DELETE (requests)"),
    ("requests", "patch", True, "HTTP PATCH (requests)"),
    ("requests", "request", True, "HTTP request (requests)"),
    # httpx — auto-instrumented (Client.send patched by dexcost)
    ("httpx", "get", True, "HTTP GET (httpx)"),
    ("httpx", "post", True, "HTTP POST (httpx)"),
    ("httpx", "put", True, "HTTP PUT (httpx)"),
    ("httpx", "delete", True, "HTTP DELETE (httpx)"),
    ("httpx", "patch", True, "HTTP PATCH (httpx)"),
    ("httpx", "request", True, "HTTP request (httpx)"),
    # aiohttp — auto-instrumented (ClientSession._request patched by dexcost)
    ("aiohttp", "get", True, "HTTP GET (aiohttp)"),
    ("aiohttp", "post", True, "HTTP POST (aiohttp)"),
    ("aiohttp", "put", True, "HTTP PUT (aiohttp)"),
    ("aiohttp", "delete", True, "HTTP DELETE (aiohttp)"),
    ("aiohttp", "request", True, "HTTP request (aiohttp)"),
    # urllib3 — auto-instrumented (HTTPConnectionPool.urlopen patched by dexcost)
    ("urllib3", "request", True, "HTTP request (urllib3)"),
]

# Constructors whose instances should inherit the module root for
# method-call matching (e.g. ``session = requests.Session()`` means
# ``session.get(...)`` is treated as a ``requests`` call).
_SESSION_CONSTRUCTORS: dict[str, str] = {
    "requests.Session": "requests",
    "httpx.Client": "httpx",
    "httpx.AsyncClient": "httpx",
    "aiohttp.ClientSession": "aiohttp",
}

# ── External service patterns ─────────────────────────────────────────
# (import_root, call_substring, category, auto_instrumented, description)
#
# Services in the dexcost 163-service catalog whose SDKs make HTTP calls
# via requests/httpx are auto-captured at runtime through domain matching.
# Services using custom transports (gRPC, raw sockets) require manual
# record_cost().

_SERVICE_PATTERNS: list[tuple[str, str, str, bool, str]] = [
    # --- Cloud / Infrastructure ---
    ("boto3", "client", "aws", True, "AWS SDK client call"),
    ("boto3", "resource", "aws", True, "AWS SDK resource call"),
    ("google.cloud", "client", "gcp", True, "Google Cloud client call"),

    # --- Vector databases (HTTP-based — auto-captured via domain matching) ---
    ("pinecone", "query", "vector_db", True, "Pinecone query"),
    ("pinecone", "upsert", "vector_db", True, "Pinecone upsert"),
    ("pinecone", "delete", "vector_db", True, "Pinecone delete"),
    ("weaviate", "query", "vector_db", True, "Weaviate query"),
    ("chromadb", "query", "vector_db", False, "ChromaDB query"),
    ("chromadb", "add", "vector_db", False, "ChromaDB add"),
    ("qdrant_client", "search", "vector_db", True, "Qdrant search"),
    ("qdrant_client", "upsert", "vector_db", True, "Qdrant upsert"),
    ("milvus", "search", "vector_db", True, "Milvus search"),
    ("milvus", "insert", "vector_db", True, "Milvus insert"),

    # --- Databases (local/custom transport — not auto-captured) ---
    ("pymongo", "find", "database", False, "MongoDB find"),
    ("pymongo", "insert", "database", False, "MongoDB insert"),
    ("pymongo", "update", "database", False, "MongoDB update"),
    ("pymongo", "delete", "database", False, "MongoDB delete"),
    ("pymongo", "aggregate", "database", False, "MongoDB aggregate"),
    ("motor", "find", "database", False, "MongoDB async find (motor)"),
    ("motor", "insert", "database", False, "MongoDB async insert (motor)"),
    ("elasticsearch", "search", "search", False, "Elasticsearch search"),
    ("elasticsearch", "index", "search", False, "Elasticsearch index"),
    ("opensearchpy", "search", "search", False, "OpenSearch search"),
    ("redis", "execute_command", "cache", False, "Redis command"),
    ("redis", "get", "cache", False, "Redis get"),
    ("redis", "set", "cache", False, "Redis set"),
    ("supabase", "table", "database", True, "Supabase table operation"),
    ("supabase", "rpc", "database", True, "Supabase RPC call"),

    # --- Payments / billing (HTTP-based — in catalog) ---
    ("stripe", "create", "payment", True, "Stripe API create"),
    ("stripe", "retrieve", "payment", True, "Stripe API retrieve"),
    ("stripe", "list", "payment", True, "Stripe API list"),
    ("stripe", "modify", "payment", True, "Stripe API modify"),

    # --- Messaging / communications (HTTP-based — in catalog) ---
    ("twilio", "create", "messaging", True, "Twilio message"),
    ("twilio", "fetch", "messaging", True, "Twilio fetch"),
    ("sendgrid", "send", "messaging", True, "SendGrid email"),
    ("resend", "send", "messaging", True, "Resend email"),
    ("postmark", "send", "messaging", True, "Postmark email"),
    ("vonage", "send_message", "messaging", False, "Vonage message"),
    ("slack_sdk", "chat_postMessage", "messaging", False, "Slack message"),
    ("slack_sdk", "api_call", "messaging", False, "Slack API call"),

    # --- Geo / Maps (HTTP-based — in catalog) ---
    ("googlemaps", "geocode", "geo", True, "Google Maps geocode"),
    ("googlemaps", "directions", "geo", True, "Google Maps directions"),
    ("googlemaps", "distance_matrix", "geo", True, "Google Maps distance matrix"),
    ("googlemaps", "places", "geo", True, "Google Maps places"),
    ("mapbox", "geocode", "geo", True, "Mapbox geocode"),
    ("mapbox", "directions", "geo", True, "Mapbox directions"),

    # --- Web scraping / data (HTTP-based — in catalog) ---
    ("firecrawl", "scrape", "scraping", True, "Firecrawl scrape"),
    ("firecrawl", "crawl", "scraping", True, "Firecrawl crawl"),
    ("tavily", "search", "scraping", True, "Tavily search"),
    ("serper", "search", "scraping", True, "Serper search"),
    ("serpapi", "search", "scraping", True, "SerpAPI search"),
    ("scrapingbee", "get", "scraping", True, "ScrapingBee request"),

    # --- Document / OCR ---
    ("pypdf", "extract", "document", False, "PDF extraction"),
    ("textract", "detect_document_text", "document", True, "AWS Textract OCR"),
    ("azure.ai.formrecognizer", "begin_recognize", "document", False, "Azure Form Recognizer"),

    # --- Speech / Audio (HTTP-based — in catalog) ---
    ("openai", "audio.transcriptions.create", "speech", True, "OpenAI Whisper transcription"),
    ("openai", "audio.speech.create", "speech", True, "OpenAI TTS"),
    ("deepgram", "transcribe", "speech", True, "Deepgram transcription"),
    ("assemblyai", "transcribe", "speech", True, "AssemblyAI transcription"),

    # --- Image generation (HTTP-based — in catalog) ---
    ("openai", "images.generate", "image", True, "OpenAI DALL-E generation"),
    ("stability_sdk", "generate", "image", True, "Stability AI generation"),

    # --- Embeddings (HTTP-based — in catalog) ---
    ("openai", "embeddings.create", "embedding", True, "OpenAI embedding"),
    ("cohere", "embed", "embedding", True, "Cohere embedding"),
    ("voyageai", "embed", "embedding", True, "Voyage AI embedding"),
]

# ── Agent framework patterns ──────────────────────────────────────────
# These frameworks wrap LLM calls internally — each .invoke()/.run()
# triggers one or more paid API calls.
# (import_root, call_substring, description)

_FRAMEWORK_PATTERNS: list[tuple[str, str, str]] = [
    # LangChain / LangGraph
    ("langchain", "invoke", "LangChain invoke (LLM/chain/agent)"),
    ("langchain", "ainvoke", "LangChain async invoke"),
    ("langchain", "run", "LangChain run"),
    ("langchain", "arun", "LangChain async run"),
    ("langchain", "predict", "LangChain predict"),
    ("langchain", "apredict", "LangChain async predict"),
    ("langchain", "call", "LangChain call"),
    ("langchain_openai", "invoke", "LangChain ChatOpenAI invoke"),
    ("langchain_anthropic", "invoke", "LangChain ChatAnthropic invoke"),
    ("langchain_community", "invoke", "LangChain community model invoke"),
    ("langgraph", "invoke", "LangGraph invoke"),
    ("langgraph", "ainvoke", "LangGraph async invoke"),
    ("langgraph", "stream", "LangGraph stream"),
    # CrewAI
    ("crewai", "kickoff", "CrewAI crew kickoff"),
    ("crewai", "execute", "CrewAI task execute"),
    # AutoGen
    ("autogen", "initiate_chat", "AutoGen initiate chat"),
    ("autogen", "generate_reply", "AutoGen generate reply"),
    ("autogen", "run", "AutoGen run"),
    # LlamaIndex
    ("llama_index", "query", "LlamaIndex query"),
    ("llama_index", "chat", "LlamaIndex chat"),
    ("llama_index", "complete", "LlamaIndex complete"),
    ("llama_index", "stream_complete", "LlamaIndex stream complete"),
    ("llama_index", "achat", "LlamaIndex async chat"),
    ("llama_index", "aquery", "LlamaIndex async query"),
    # Haystack
    ("haystack", "run", "Haystack pipeline run"),
    # Semantic Kernel
    ("semantic_kernel", "invoke", "Semantic Kernel invoke"),
    # OpenAI Agents SDK
    ("agents", "Runner.run", "OpenAI Agents SDK run"),
    # Anthropic Claude Agent SDK
    ("claude_agent_sdk", "run", "Claude Agent SDK run"),
]


# ── Directories to skip ───────────────────────────────────────────────

_SKIP_DIRS = frozenset({
    ".venv", "venv", "env", ".env",
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules",
    ".tox", ".nox",
    "dist", "build", ".eggs", "*.egg-info",
    "site-packages",
})


# ── Public API ────────────────────────────────────────────────────────


def scan_directory(path: Path) -> ScanResult:
    """Scan a directory tree for cost points in .py files."""
    result = ScanResult()
    if not path.exists():
        return result

    target = path if path.is_dir() else path.parent
    py_files = sorted(
        f for f in target.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in f.parts)
    )
    for py_file in py_files:
        result.files_scanned += 1
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError, PermissionError, OSError):
            _log.debug("Skipping %s (parse error)", py_file)
            continue
        points = _analyze_file(tree, str(py_file))
        result.cost_points.extend(points)
    return result


def generate_stubs(result: ScanResult) -> str:
    """Generate best-in-class code snippets for integrating dexcost.

    The output is a self-contained integration template that shows:
    1. SDK initialisation
    2. Customer/project context
    3. Task wrapper with manual ``record_cost`` calls inside
    4. A summary of auto-instrumented providers (no code changes needed)
    """
    if not result.cost_points:
        return ""

    auto_points = [cp for cp in result.cost_points if cp.auto_instrumented]
    manual_points = [cp for cp in result.cost_points if not cp.auto_instrumented]

    lines: list[str] = [
        "# ============================================================",
        "# dexcost integration stubs",
        "# Generated by: dexcost scan --generate-stubs",
        "# ============================================================",
        "",
        "# --- Step 1: Initialize dexcost ---",
        "import dexcost",
        "from decimal import Decimal",
        "",
        'dexcost.init(api_key="dx_live_...")  # or set DEXCOST_API_KEY env var',
        "",
        "# --- Step 2: Set customer context (in your request handler) ---",
        "dexcost.set_context(",
        '    customer_id="your_customer_id",',
        '    project_id="your_project_id",',
        ")",
        "",
        "# --- Step 3: Track tasks ---",
        'with dexcost.task(task_type="your_task_type") as t:',
        "    # Your agent code here...",
    ]

    if manual_points:
        lines.append("")
        lines.append(
            "    # --- Manual cost tracking "
            "(for services not auto-instrumented) ---"
        )
        # Group manual cost points by file for readability
        by_file: dict[str, list[CostPoint]] = {}
        for cp in manual_points:
            by_file.setdefault(cp.file, []).append(cp)
        for file_points in by_file.values():
            for cp in file_points:
                lines.append(f"    # {cp.file}:{cp.line} — {cp.description}")
                lines.append(
                    f'    t.record_cost("{cp.import_name}", '
                    f'cost_usd=Decimal("0.00"))  # TODO: set actual cost'
                )
                lines.append("")

    if auto_points:
        # Ensure the task block is closed before the auto summary
        lines.append("")
        lines.append("# --- Auto-instrumented (no code changes needed) ---")
        # Aggregate by import_name and count occurrences
        provider_counts: dict[str, int] = {}
        for cp in auto_points:
            provider_counts[cp.import_name] = (
                provider_counts.get(cp.import_name, 0) + 1
            )
        for provider, count in provider_counts.items():
            suffix = "s" if count > 1 else ""
            lines.append(
                f"# \u2713 {provider} ({count} call{suffix} detected)"
            )

    return "\n".join(lines) + "\n"


# ── AST analysis engine ───────────────────────────────────────────────


def _analyze_file(tree: ast.Module, filename: str) -> list[CostPoint]:
    """Walk AST to find imports, track assignments, and match call sites."""

    # Step 1: Collect imported module roots (handles aliases)
    imported: set[str] = set()
    # Maps import alias → full module path:  e.g. {"oai": "openai"}
    import_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                imported.add(root)
                if alias.asname:
                    import_aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            imported.add(root)
            for alias in node.names:
                if alias.asname:
                    import_aliases[alias.asname] = f"{node.module}.{alias.name}"
                else:
                    import_aliases[alias.name] = f"{node.module}.{alias.name}"

    # Step 2: Track variable assignments to resolve constructor origins.
    # e.g.  ``client = openai.OpenAI()``  →  var_origins["client"] = "openai"
    #        ``session = requests.Session()``  →  var_origins["session"] = "requests"
    var_origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                constructor = _resolve_call(node.value.func)
                if constructor:
                    # Check if it's a known session/client constructor
                    for ctor_pattern, origin_module in _SESSION_CONSTRUCTORS.items():
                        if ctor_pattern in constructor:
                            var_origins[target.id] = origin_module
                            break
                    else:
                        # Generic: ``client = openai.OpenAI()`` → root is "openai"
                        ctor_root = constructor.split(".")[0]
                        # Resolve aliases: if ``import openai as oai``, ctor_root might be "oai"
                        # Keep the full resolved path (e.g. "google.generativeai")
                        # so multi-part prefix matching works correctly.
                        resolved_full = import_aliases.get(ctor_root, ctor_root)
                        module_root = resolved_full.split(".")[0]
                        if module_root in imported:
                            var_origins[target.id] = resolved_full

    # Step 3: Walk calls and match against all pattern tables
    points: list[CostPoint] = []
    seen_lines: set[int] = set()  # Avoid duplicate detections on same line

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_str = _resolve_call(node.func)
        if not call_str:
            continue

        if node.lineno in seen_lines:
            continue

        # Resolve the call root through var_origins for assignment tracking.
        # e.g. call_str="client.chat.completions.create" with
        # var_origins={"client": "openai"} → effective root is "openai"
        call_root = call_str.split(".")[0]
        effective_root = var_origins.get(call_root, call_root)
        # Also resolve import aliases on the call root directly.
        # Keep the full module path for multi-part prefix matching
        # (e.g. "genai" → "google.generativeai", not just "google").
        effective_full = import_aliases.get(effective_root, effective_root)
        if effective_root in import_aliases:
            effective_root = effective_full.split(".")[0]

        # --- Check LLM patterns ---
        matched = False
        for prefix, pattern, auto, desc in _LLM_PATTERNS:
            root = prefix.split(".")[0]
            # For multi-part prefixes, require full prefix match to avoid
            # e.g. "google.genai" matching "google.cloud" patterns.
            if "." in prefix:
                prefix_match = (
                    prefix in imported
                    or prefix in call_str
                    or effective_full == prefix
                    or effective_full.startswith(prefix + ".")
                )
            else:
                prefix_match = (root in imported or effective_root == root)
            if prefix_match and pattern in call_str:
                points.append(CostPoint(
                    file=filename, line=node.lineno,
                    category="llm", auto_instrumented=auto,
                    description=desc, import_name=prefix,
                ))
                seen_lines.add(node.lineno)
                matched = True
                break

        if matched:
            continue

        # --- Check framework patterns ---
        for prefix, pattern, desc in _FRAMEWORK_PATTERNS:
            root = prefix.split(".")[0]
            if (root in imported or effective_root == root) and pattern in call_str:
                points.append(CostPoint(
                    file=filename, line=node.lineno,
                    category="framework", auto_instrumented=False,
                    description=desc, import_name=prefix,
                ))
                seen_lines.add(node.lineno)
                matched = True
                break

        if matched:
            continue

        # --- Check HTTP patterns ---
        # Match if: (a) direct call like ``requests.get(...)`` or
        #           (b) session call like ``session.get(...)`` where session
        #               was assigned from ``requests.Session()``.
        for prefix, method, auto, desc in _HTTP_PATTERNS:
            if method not in call_str:
                continue
            # Direct call: ``requests.get(...)``
            if prefix in imported and prefix in call_str:
                points.append(CostPoint(
                    file=filename, line=node.lineno,
                    category="http", auto_instrumented=auto,
                    description=desc, import_name=prefix,
                ))
                seen_lines.add(node.lineno)
                matched = True
                break
            # Session/client call: variable root resolves to this http lib
            if effective_root == prefix and call_root != prefix:
                points.append(CostPoint(
                    file=filename, line=node.lineno,
                    category="http", auto_instrumented=auto,
                    description=desc, import_name=prefix,
                ))
                seen_lines.add(node.lineno)
                matched = True
                break

        if matched:
            continue

        # --- Check service patterns ---
        for prefix, pattern, cat, auto, desc in _SERVICE_PATTERNS:
            root = prefix.split(".")[0]
            # For multi-part prefixes (e.g. "google.cloud"), verify the full
            # prefix appears in the imported set or call string — not just the
            # top-level package.  This prevents "google.genai" from matching
            # "google.cloud" patterns.
            if "." in prefix:
                prefix_match = (
                    prefix in imported
                    or prefix in call_str
                    or effective_full == prefix
                    or effective_full.startswith(prefix + ".")
                )
            else:
                prefix_match = (root in imported or effective_root == root)
            if prefix_match and pattern in call_str:
                points.append(CostPoint(
                    file=filename, line=node.lineno,
                    category=cat, auto_instrumented=auto,
                    description=desc, import_name=prefix,
                ))
                seen_lines.add(node.lineno)
                break

    return points


def _resolve_call(node: Any) -> str | None:
    """Attempt to resolve a call's function name as dotted string."""
    parts: list[str] = []
    current: Any = node
    depth = 0
    while depth < 20:  # Guard against pathological AST depth
        depth += 1
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Name):
            parts.append(current.id)
            break
        elif isinstance(current, ast.Call):
            # e.g. boto3.client('s3').put_object() — resolve inner call
            inner = _resolve_call(current.func)
            if inner:
                parts.append(inner)
            break
        elif isinstance(current, ast.Subscript):
            # e.g. collection["name"].find()
            current = current.value
        else:
            return None
    parts.reverse()
    return ".".join(parts)
