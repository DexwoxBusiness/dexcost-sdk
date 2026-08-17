# dexcost

**Agent Unit Economics SDK** — track end-to-end business-task costs for AI agents.

dexcost attributes LLM calls, non-LLM service fees, and retry waste to customers, projects, and workflows so you can answer *"what does each AI task actually cost?"*

## Install

```bash
pip install dexcost
```

With all LLM provider SDKs:

```bash
pip install dexcost[all]
```

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

## Auto-Instrumentation

dexcost auto-instruments **6 LLM providers** and **5 HTTP libraries**.

### LLM Providers

| Provider | Package | Auto-Patched Method |
|----------|---------|-------------------|
| OpenAI | `openai` | Chat Completions and Responses `create` (sync + async, including streams) |
| Anthropic | `anthropic` | `messages.create` (sync + async) |
| LiteLLM | `litellm` | `completion` / `acompletion` |
| Google Gemini | `google-genai` | `models.generate_content` |
| AWS Bedrock | `boto3` (botocore) | `invoke_model` |
| Cohere | `cohere` | `chat` / `chat_stream` (sync + async) |

Every LLM call inside a tracked task is captured automatically — cost, tokens, latency, model, provider. No manual `record_llm_call` needed.

### HTTP Libraries (Non-LLM Cost Capture)

| Library | What's Patched |
|---------|---------------|
| `requests` | `Session.send` |
| `httpx` | `Client.send` |
| `aiohttp` | `ClientSession._request` |
| `botocore` (boto3) | `URLLib3Session.send` |
| `urllib3` | `HTTPConnectionPool.urlopen` |

HTTP calls to domains in the [163-service catalog](src/dexcost/data/service_prices.json) (Pinecone, Twilio, SendGrid, Stripe, Firecrawl, Exa, etc.) are automatically captured as `external_cost` events with cost extracted from the response.

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
| `auto_instrument` | `list[str]` | All 6 providers | Which LLM SDKs to patch |
| `track_http` | `bool` | `True` | Patch HTTP libraries for non-LLM cost capture |
| `batch_size` | `int` | `100` | Events per sync batch |
| `flush_interval` | `float` | `5.0` | Seconds between sync pushes |
| `redact_fields` | `list[str]` | `None` | Field names to redact from event details |
| `hash_customer_id` | `bool` | `False` | SHA-256 hash customer_id before storage |
| `environment` | `str` | `None` | Set to `"development"` for dev console mode |
| `storage` | `str` | `None` | Storage mode (`"local"` or auto-detect) |
| `endpoint` | `str` | `https://api.dexcost.io` | Control Layer URL. Must start with `http://` or `https://`. The **only** way to override the endpoint — it is not read from the environment. |
| `buffer_path` | `str` | `~/.dexcost/buffer.db` | Path to local SQLite buffer |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEXCOST_API_KEY` | API key (if not passed to `init()`) |
| `DEXCOST_ENV` | Set to `development` for dev console output |

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
dexcost.set_context(customer_id="acme-corp", project_id="proj-alpha")

# All tasks created after this inherit customer_id and project_id
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
`fix(python): ...` for fixes. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
