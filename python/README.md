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

## Auto-Instrumentation

dexcost auto-instruments **6 LLM providers** and **5 HTTP libraries**.

### LLM Providers

| Provider | Package | Auto-Patched Method |
|----------|---------|-------------------|
| OpenAI | `openai` | `chat.completions.create` (sync + async) |
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
