# dexcost

**Agent Unit Economics SDK** — track end-to-end business-task costs for AI agents.

dexcost attributes LLM calls, non-LLM service fees, and retry waste to customers, projects, and workflows so you can answer *"what does each AI task actually cost?"*

## Install

```bash
pip install dexcost
```

With every supported provider, framework, and GPU integration:

```bash
pip install dexcost[all]
```

Or install only the provider integration you use:

```bash
pip install "dexcost[openai]"
pip install "dexcost[anthropic]"
pip install "dexcost[litellm]"
pip install "dexcost[gemini]"
pip install "dexcost[bedrock]"
pip install "dexcost[cohere]"
pip install "dexcost[mcp]"
pip install "dexcost[ollama]"
pip install "dexcost[openrouter]"
pip install "dexcost[perplexity]"
pip install "dexcost[fal]"
pip install "dexcost[gpu]"  # NVIDIA NVML task-level GPU accounting
```

The Gemini extra currently supports `google-genai` 1.x and 2.x and is bounded
to `<3.0.0`, matching Google's announced next-major breaking-change boundary.

## Quick Start

### Global API (recommended)

```python
import dexcost

dexcost.init(api_key="dx_live_...")  # or set DEXCOST_API_KEY env var
dexcost.set_context(customer_id="acme-corp")

with dexcost.task(task_type="summarise_doc") as t:
    # LLM calls are auto-captured — just use OpenAI/Anthropic/etc normally
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Summarise this document"}],
    )

    # Record non-LLM costs manually
    t.record_cost(service="pdf_parser", cost_usd="0.002")

    # Record an actual business result explicitly; task success alone is not
    # treated as an achieved outcome.
    t.record_outcome("document_delivered", value=True)

dexcost.close()
```

### Instance API (for multi-tracker scenarios)

```python
from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

tracker = CostTracker(storage=SQLiteStorage("/tmp/demo.db"))

with tracker.task(task_type="summarise_doc", customer_id="acme") as t:
    t.record_llm_call("openai", "gpt-4o", input_tokens=800, output_tokens=150)
    t.record_cost(service="pdf_parser", cost_usd="0.002")
```

## CrewAI and Griptape

Install only the framework integration you use:

```bash
pip install "dexcost[crewai]"
pip install "dexcost[griptape]"
```

The wrappers preserve the original framework object and public method
signatures. They create one canonical DexCost task when no task is active, or
reuse the caller's active task. Sync, async, streaming, failure, cancellation,
early-close, and framework-native tool events share that same lifecycle.

```python
import dexcost

dexcost.init(api_key="dx_live_...")

# Existing CrewAI Crew, Agent, LiteAgent, and Flow objects are supported.
tracked_crew = dexcost.track_crewai(crew)
crew_output = tracked_crew.kickoff(inputs={"topic": "unit economics"})

# Existing Griptape Structure objects are supported without replacing drivers.
tracked_agent = dexcost.track_griptape(agent)
tracked_agent.run("Build the report")
```

For an instance tracker, pass it as the second argument:

```python
from dexcost.integrations import track_crewai, track_griptape

tracked_crew = track_crewai(crew, tracker)
tracked_structure = track_griptape(structure, tracker)
```

Provider instrumentation is the authoritative LLM-cost path and prevents
double counting. Framework event fallback is opt-in for custom providers that
DexCost cannot instrument:

```python
tracked_crew = dexcost.track_crewai(crew, capture_llm_events=True)
```

Do not enable that fallback for the same calls already captured by OpenAI,
Anthropic, LiteLLM, Gemini, Bedrock, or another provider instrument. Native
tool capture stores only bounded tool identity, opaque framework IDs, status,
cache/attempt dimensions, and exact duration when the framework exposes it.
Prompts, tool arguments, chain-of-thought, outputs, and error messages are not
stored. See [FRAMEWORK-COMPATIBILITY.md](FRAMEWORK-COMPATIBILITY.md) for the
current execution surface and compatibility evidence.

## Business outcomes

Outcomes are durable, revisioned business facts linked to a task. DexCost does
not infer them from a successful task because completing technical work is not
the same as achieving a customer result.

```python
from decimal import Decimal

with dexcost.task(task_type="campaign_export") as task:
    # ...generate and export the campaign...
    exported = task.record_outcome("campaign_exported", value=True)
    task.record_outcome("approved_creative_count", value=2)
    task.record_outcome("quality_score", value=Decimal("0.92"))

# Correct a previously recorded outcome by preserving its identity and
# incrementing the revision. The local ledger rejects gaps and invalid state
# transitions before they reach the control plane.
dexcost.record_outcome(
    "campaign_exported",
    task_id=task.task_id,
    outcome_id=exported.outcome_id,
    revision=2,
    state="missed",
    value=False,
)
```

Values are exact typed facts: Python strings, booleans, integers, and
`Decimal` values map to the corresponding wire types. Use canonical lowercase
names such as `campaign_exported`; do not place secrets or personal data in an
outcome name or value.

## Agent and workflow identity

Keep the business workflow, deployed agent, and technical task type as separate
dimensions. Set the stable identity once; nested tasks inherit it automatically.

```python
dexcost.set_context(
    customer_id="dexcost-internal",
    project_id="dexcost-marketing-campaign",
    agent="campaign_director",
    agent_version="demo-v1",
    workflow_id="campaign_generation",
    workflow_session_id="campaign-2026-08-17",
)

with dexcost.task(task_type="campaign.run") as campaign:
    with dexcost.task(task_type="campaign.script.generate"):
        generate_script()

    with dexcost.task(task_type="campaign.narration.generate"):
        generate_narration()

    campaign.record_outcome("campaign_exported", value=True)
```

This produces one canonical campaign hierarchy while preserving the actual
work-step task types. Agent identity never replaces `task_type`, and technical
success is not presented as a business outcome unless the application records
one explicitly.

## Attribution observation fields (contract v3)

Observations are emitted on the `schema_version: "3"` wire contract. The
following optional fields were added to v3 in place — old payloads that omit
them stay valid, and the bundled JSON schema
(`dexcost/attribution/attribution-v3-schema.json`) is validated strictly on
every emit.

| Field | Type | Rule | Source |
|-------|------|------|--------|
| `environment` | `str` | `^[a-z0-9][a-z0-9._-]{0,63}$` (max 64) | `init(environment=...)` / `DEXCOST_ENV` |
| `operation.latency_ms` | `int` | `0`–`86400000` whole milliseconds | Measured call latency |
| `operation.error` | `object` | `{type, code?}`; rejected when `operation.status == "succeeded"` | Instrument failure marker |
| `operation.error.type` | `str` | `^[a-z0-9][a-z0-9._-]{0,127}$` — canonical taxonomy (`timeout`, `rate_limit`) | Instrument failure marker |
| `operation.error.code` | `str` | 1–64 chars, opaque provider code | Provider response |
| `resource.type` | `enum` | `model`, `sku`, `instance`, `endpoint`, `session`, `tool`, `other` | `"tool"` covers MCP/agent tool calls |
| `assignment.user_id` | `str` | 1–512 chars, opaque | `set_context(user_id=...)` |
| `assignment.product_id` | `str` | 1–512 chars, opaque | `set_context(product_id=...)` |

```python
dexcost.init(environment="production")  # emitted as observation.environment

dexcost.set_context(
    customer_id="acme-corp",
    project_id="proj-alpha",
    user_id="user-42",              # the end user the work is performed for
    product_id="support-console",   # the product surface driving the work
)
```

A failed tool call therefore reaches the control plane as:

```json
{
  "schema_version": "3",
  "environment": "production",
  "component": "external",
  "resource": { "type": "tool", "id": "web_browser" },
  "operation": {
    "status": "failed",
    "latency_ms": 1250,
    "error": { "type": "timeout", "code": "ETIMEDOUT" }
  },
  "usage_snapshot": "full",
  "usage": []
}
```

A succeeded operation must not carry `operation.error`; the SDK rejects that
observation locally instead of shipping it.

## Auto-Instrumentation

dexcost auto-instruments **10 AI provider SDKs**, the MCP tool client, and **5 HTTP libraries**.

### LLM Providers

| Provider | Package | Auto-Patched Method |
|----------|---------|-------------------|
| OpenAI / Azure / compatible gateways | `openai` | Chat/legacy Completions, structured `parse`, Responses, embeddings, image generation/edit/variation, audio transcription/translation/speech, Azure host/deployment routing, and OpenRouter/Perplexity-compatible routing (sync + async, including native streams) |
| Anthropic | `anthropic` | Current `messages.create` sync/async, native streams, cache buckets, and tool calls |
| LiteLLM | `litellm` | Language/Responses, embeddings, image generation/edit/variation, transcription/speech, rerank, moderation, search, OCR, and background Responses/video/batch/fine-tuning jobs (sync + async where exposed, including terminal stream and job reconciliation) |
| Google Gemini / Enterprise | `google-genai` | `Models` and `AsyncModels`: content generation/streaming, embeddings, image generation/upscale/edit/recontext/segmentation; foreground Interactions sync/async/SSE |
| AWS Bedrock | `boto3`/botocore; official Smithy runtime on Python 3.12+ | Current Converse/streams; InvokeModel chat, embeddings, images, and rerank; guardrails, CountTokens, durable async media jobs, and Nova Sonic bidirectional speech; exact regional/profile identity, cache TTL buckets, tools, routing, service-tier/latency dimensions, and private ARN hashing |
| Cohere | `cohere` | V1/V2 `chat`, `chat_stream`, `embed`, and `rerank` (sync + async) |
| Ollama | `ollama` | Module singleton, `Client`, and `AsyncClient` chat/generate streams, current and legacy embeddings, web search, and web fetch |
| OpenRouter | `openrouter` | Chat, Responses, embeddings, images, STT, TTS, rerank, video jobs, and generation-cost reconciliation (sync + async and native streams) |
| Perplexity | `perplexityai` | Agent Responses and background jobs, Sonar chat, Search, embeddings/contextualized embeddings, and native streams (sync + async) |
| fal.ai | `fal-client` | Module/client run, subscribe, stream, and durable queue submit/status/result/cancel (sync + async) |

Every supported AI call inside a tracked task is captured automatically.
LiteLLM also preserves canonical routed-provider identity for OpenAI,
Anthropic, Google/Vertex, Azure/Azure AI, Bedrock, Cohere, Hugging Face,
Together, Ollama, Mistral, Groq, OpenRouter, Perplexity, fal.ai, xAI,
DeepSeek, Fireworks AI, Nvidia NIM, nano-gpt, and other explicit LiteLLM
provider/model routes. OpenAI and Google multimodal paths preserve native
text/image/audio/video/cache,
reasoning, tool-input, character, and media-count quantities, calculate against
the active catalog, and retain only quantities and opaque provider IDs. Prompts,
media, transcripts, tool payloads, and generated output are not stored. Google
stream success is recorded only on natural completion; early close is cancelled
and stream exceptions are failed.

### HTTP Libraries (Non-LLM Cost Capture)

| Library | What's Patched |
|---------|---------------|
| `requests` | `Session.send` |
| `httpx` | `Client.send` |
| `aiohttp` | `ClientSession._request` |
| `botocore` (boto3) | `URLLib3Session.send` |
| `urllib3` | `HTTPConnectionPool.urlopen` |

HTTP calls matching the active service catalog (Pinecone, Twilio, SendGrid,
Stripe, Firecrawl, Exa, etc.) are automatically captured as `external_cost`
events with cost extracted from the response. Catalog releases are distributed
by the control plane as immutable, content-addressed artifacts and evaluated
locally. The SDK validates and caches a last-known-good release for offline use;
the bundled catalog remains an emergency bootstrap until Python and TypeScript
joint migration gates allow it to be reduced safely.

For signed authority, configure rotated Ed25519 public keys and require a
signature. Keys are raw 32-byte public keys encoded as unpadded base64url.
Supplying keys requires signatures by default; setting the boolean explicitly
is shown for clarity:

```python
dexcost.init(
    api_key="dx_live_...",
    catalog_trusted_keys={"dexcost-prod-2026-01": "<public-key-base64url>"},
    catalog_require_signature=True,
)
```

Air-gapped hosts use the same validation and activation path:

```python
dexcost.init(
    storage="local",
    catalog_trusted_keys={"dexcost-prod-2026-01": "<public-key-base64url>"},
    catalog_require_signature=True,
)
dexcost.import_catalog_bundle("dexcost-catalog-release.dcr.json")
```

Import never bypasses signature, expiry, downgrade, size, hash, schema, or
semantic checks. The previous release remains available if import fails.
When the packaged production trust document has no keys, remote catalog refresh
and bundle activation stay disabled and bundled pricing remains active. This
state is exposed by `dexcost.catalog_status().signature_verification` as
`"disabled_no_trust"`; an empty trust document is never treated as permission
to accept unsigned catalogs. `catalog_require_signature=False` is an explicit,
temporary migration override and enables unsigned refresh deliberately.

### Controlling Instrumentation

```python
# Instrument only specific providers
dexcost.init(auto_instrument=["openai", "gemini"])

# Disable all auto-instrumentation
dexcost.init(auto_instrument=[])

# Disable HTTP tracking
dexcost.init(track_http=False)
```

## Configuration

### `dexcost.init()` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `DEXCOST_API_KEY` env | API key for cloud push |
| `auto_instrument` | `list[str]` | All supported instruments | Which provider/tool SDKs to patch |
| `track_http` | `bool` | `True` | Patch HTTP libraries for non-LLM cost capture |
| `batch_size` | `int` | `100` | Events per sync batch |
| `flush_interval` | `float` | `5.0` | Seconds between sync pushes |
| `catalog_trusted_keys` | `Mapping[str, str \| bytes]` | env or packaged production trust | Rotated Ed25519 public keys by manifest key ID |
| `catalog_require_signature` | `bool \| None` | `True` with keys; remote refresh disabled without keys | Reject unsigned network releases and durable cache entries; `False` is an explicit migration override |
| `catalog_refresh_interval` | `float` | 24 hours | Background release refresh interval in seconds |
| `catalog_refresh_jitter` | `float` | `0.1` | Random refresh spread from 0 through 0.5 |
| `redact_fields` | `list[str]` | `None` | Field names to redact from event details |
| `hash_customer_id` | `bool` | `False` | SHA-256 hash customer_id before storage |
| `environment` | `str` | `None` | Deployment environment (`"production"`, `"staging"`, …), emitted as `observation.environment`. `"development"` also enables dev console mode |
| `storage` | `str` | `None` | Storage mode (`"local"` or auto-detect) |
| `endpoint` | `str` | `https://api.dexcost.io` | Control Layer URL. Must start with `http://` or `https://`. The **only** way to override the endpoint — it is not read from the environment. |
| `buffer_path` | `str` | `~/.dexcost/buffer.db` | Path to local SQLite buffer |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEXCOST_API_KEY` | API key (if not passed to `init()`) |
| `DEXCOST_ENV` | Deployment environment emitted on every observation. Set to `development` for dev console output |
| `DEXCOST_CATALOG_TRUSTED_KEYS` | Strict JSON object mapping 1–8 key IDs to unpadded base64url Ed25519 public keys; ignored when `catalog_trusted_keys` is passed |
| `DEXCOST_CATALOG_REQUIRE_SIGNATURE` | Strict `true` or `false`; used only when `catalog_require_signature` is omitted. Defaults to `true` with trusted keys; without keys remote refresh remains disabled unless this is explicitly `false` |

Catalog trust resolves from an explicit option, then the environment, then the
public trust document shipped in the package. Private signing keys are never
part of an SDK or SDK environment.

> **Note:** `DEXCOST_ENDPOINT` is **no longer read**. The Control Layer URL is
> configured only via `init(endpoint="https://...")` (default
> `https://api.dexcost.io`). This prevents an attacker who controls the process
> environment from redirecting telemetry and the Bearer API key to a hostile
> collector.

## Task Tracking

### Context Manager

```python
with dexcost.task(task_type="resolve_ticket") as t:
    # All LLM/HTTP calls inside are automatically captured
    pass
```

### Decorator

```python
@tracker.track_task(task_type="generate_report", customer_id="acme")
def generate_report(data):
    # LLM calls here are tracked
    pass
```

### Manual Start/End

```python
t = tracker.start_task(task_type="batch_job", customer_id="acme")
# ... do work ...
t.end(status="success")
```

### Cross-process campaign or workflow hierarchy

Use stable UUIDs when a workflow spans workers or short-lived tool processes.
Supplying `root_task_id` opts the task into the revisioned business-identity
contract. Create the root once, then pass both root and parent IDs to children:

```python
import uuid

campaign_root = uuid.uuid5(uuid.NAMESPACE_URL, "campaign:dexcost-launch")

with dexcost.task(
    task_type="campaign.run",
    task_id=campaign_root,
    root_task_id=campaign_root,
    experiment_id="creative-angle",
):
    pass

# This may execute in a different process.
with dexcost.task(
    task_type="campaign.scene.render",
    root_task_id=campaign_root,
    parent_task_id=campaign_root,
    experiment_id="creative-angle",
    variant="proof-first",
):
    render_scene()
```

The identity snapshot is published with the final task update. Task type and
assignment fields are immutable for that SDK task identity. The Python SDK
currently emits revision 1 only; later corrections belong in the workspace
business-attribution API rather than rewriting provider usage.

### Local NVIDIA GPU usage

Opt in only on the leaf task that owns the GPU work:

```python
with tracker.task(task_type="local_whisper", track_gpu=True):
    transcribe_on_local_gpu()
```

DexCost records measured GPU-seconds, the normalized device model, and
utilization evidence. Without an explicit user-owned rate, a local GPU and
local network transfer remain unpriced; the SDK does not apply public-cloud
fallbacks to locally owned infrastructure.

To attribute your own amortized hardware, electricity, hosting, or bandwidth
rate, load a versioned YAML file during initialization. The values below are
illustrative user inputs, not DexCost public-list prices:

```yaml
version: 2
rates: {}
infrastructure:
  gpu:
    nvidia-geforce-rtx-5060-ti:
      per: gpu_hour
      cost_usd: "0.25"
  network:
    local:
      per: gb_transferred
      cost_usd: "0.02"
```

```python
dexcost.init(
    api_key="dx_live_...",
    rates_path="rates.yaml",
)
```

GPU units can be ``gpu_second`` or ``gpu_hour``. Network units can be
``gb_transferred`` (request plus response bytes) or ``gb_egress``. Keys are
normalized and matched exactly; there is no default-rate fallback. Configured
costs carry ``sdk_rate_registry`` evidence and a deterministic pricing version.
Do not enable GPU measurement on both a parent and its child, because they
would measure the same hardware interval twice.

## TrackedTask Methods

```python
with dexcost.task(task_type="...") as t:
    # Record LLM call manually (usually auto-captured)
    t.record_llm_call("openai", "gpt-4o", input_tokens=800, output_tokens=150)

    # Record non-LLM cost
    t.record_cost(service="pinecone", cost_usd="0.001")

    # Record usage (cost computed from registered rates)
    t.record_usage(service="s3_storage", units=1024)

    # Mark a retry
    t.mark_retry(reason="rate_limit", cost_usd="0.005")

    # Link to external trace
    t.link_trace(provider="datadog", trace_id="abc123")
```

## Customer Attribution

```python
dexcost.set_context(
    customer_id="acme-corp",
    project_id="proj-alpha",
    user_id="user-42",
    product_id="support-console",
)

# All tasks created after this inherit customer_id, project_id, user_id and
# product_id; they are shipped as the business identity `assignment` snapshot.
with dexcost.task(task_type="...") as t:
    pass  # t.task.customer_id == "acme-corp"
```

## Dev Mode

Set `DEXCOST_ENV=development` or pass `environment="development"` to `init()`. In dev mode:
- Cost events are printed to the terminal
- No data is pushed to the cloud
- Useful for local development and debugging

## CLI

```bash
dexcost status          # DB location, event count, sync status
dexcost rates --list    # Show registered cost rates
dexcost scan .          # Find untracked cost points in your code
dexcost scan . --generate-stubs  # Generate record_cost() stubs for manual points
```

## Development

```bash
pip install -e ".[all]"
pip install ruff black mypy pytest

make lint        # ruff
make format      # black
make typecheck   # mypy strict
make test        # pytest
```

## Releases

Releases are generated from Conventional Commit pull-request titles and are
squash-merged to `main`. Use `feat(python): ...` for features and
`fix(python): ...` for fixes. See [CONTRIBUTING.md](../CONTRIBUTING.md) and
[CHANGELOG.md](CHANGELOG.md).

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
