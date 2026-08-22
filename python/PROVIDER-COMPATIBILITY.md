# Python provider and framework compatibility

Status: Python-first parity completion gate
Last verified: 2026-08-22

This is the source of truth for the Python contract that TypeScript must copy.
The comparison boundary is every provider and framework attribution path found
in the audited Revenium Python and Node SDKs. DexCost also keeps its existing
capabilities and the additional first-class providers listed below. A provider
is not complete merely because its name can be detected.

## Completion contract

Every supported adapter must satisfy all applicable rows:

| Concern | Required behavior |
|---|---|
| Current API | Verify the surface against the current official package, protocol, and provider documentation. |
| Task ownership | Reuse an active task or create exactly one auto-task; release invocation context without losing stream or job ownership. |
| Capability | Snapshot the active capability identity at invocation, including streams and delayed jobs. |
| Idempotency | Snapshot caller idempotency for immediate operations; persist only its SHA-256 digest. Provider job IDs are the durable delayed-work identity. |
| Sync and async | Preserve native return types, awaitability, iteration, context managers, and exception identity. |
| Streams | Succeed only on natural terminal completion. Early close is cancelled, a raised stream is failed, and an operation is recorded at most once. |
| Delayed work | Persist a revisioned submitted/running/terminal provider job and reconcile by opaque provider ID without double counting. |
| Usage | Retain exact quantities, bounded opaque IDs, and safe billing dimensions only. Never retain prompts, messages, outputs, media, vectors, URLs, tool payloads, or error messages. |
| Pricing | Prefer provider-reported cost when available; otherwise use the active server catalog release locally. Unknown evidence stays unknown. |
| Wire | Emit attribution v3 decimal multi-meter usage with explicit operation status, provider, service, resource, capability, and pricing provenance. |
| Installation | Optional extra, auto-instrument list, scanner, public exports, docs, and runtime patching must agree. |
| Evidence | Exercise the installed official package or real protocol, mocking only the network boundary. |

The shared immediate/stream implementation is
`src/dexcost/instruments/_provider_metering.py`; delayed lifecycle and revision
deduplication are in `src/dexcost/provider_jobs.py`.

## Revenium provider closure

The following is the audited union of provider identity emitted directly by
Revenium Python and Node. `Verified` means DexCost has an attribution path and
current compatibility evidence for that route.

| Revenium-attributed route | DexCost route | Status | DexCost advantage retained |
|---|---|---|---|
| OpenAI | native OpenAI | Verified | Complete inference/multimodal usage, streams, realtime, delayed jobs, exact provider cost, privacy, outcomes linkage. |
| Azure OpenAI | OpenAI-compatible Azure host routing | Verified | Both current Azure host forms, deployment dimension, canonical `azure/...` model identity. |
| Anthropic | native Anthropic | Verified | Sync/async/stream attribution, cache usage, failures, private quantities-only storage. |
| Amazon Bedrock | native botocore/Bedrock | Verified | Provider/model normalization, streamed usage, region-safe pricing candidates. |
| Google AI / Gemini | current `google-genai` | Verified | Sync/async, content, streams, embeddings, image operations, Interactions, multimodal meters, jobs. |
| Vertex AI | `google-genai` Vertex mode | Verified | Current supported Vertex path with provider/service identity and the same lifecycle contract. |
| Ollama | native Ollama | Verified | Module/client/async client, streams, embeddings, web search/fetch, local/hosted distinction, native duration meters. |
| LiteLLM | native LiteLLM | Verified | Exact provider cost, cache/reasoning/tool meters, stream ownership, and canonical routed-provider identity. |
| Perplexity | native and OpenAI-compatible | Verified | Agent Responses, background reconciliation, Sonar chat, Search, embeddings, exact provider cost, streams. |
| fal.ai | native `fal-client` | Verified | Run/subscribe/stream plus durable queue submit/status/result/cancel, media quantities, no guessed duration. |
| Cohere | native Cohere | Verified | Chat and chat stream sync/async attribution retained. |
| Hugging Face | LiteLLM routed provider | Verified | Canonical `huggingface/...` identity. |
| Together AI | LiteLLM routed provider | Verified | Canonical `together_ai/...` identity. |
| Mistral | LiteLLM routed provider | Verified | Canonical `mistral/...` identity. |
| Groq | LiteLLM routed provider | Verified | Canonical `groq/...` identity. |

DexCost additionally supports first-class OpenRouter and MCP attribution; these
are not removed merely because one Revenium SDK lacks the same direct adapter.

## Provider surfaces

### OpenAI, Azure OpenAI, and OpenAI-compatible gateways

Official sources:

- <https://github.com/openai/openai-python/blob/main/api.md>
- <https://platform.openai.com/docs/pricing>
- <https://learn.microsoft.com/azure/ai-foundry/openai/how-to/switching-endpoints>
- <https://docs.perplexity.ai/guides/chat-completions-sdk>

Verified against `openai==2.54.0`. DexCost covers sync/async Chat and legacy
Completions, structured parse helpers, Responses, embeddings, image
generation/edit/variation, transcription/translation/speech, native streams,
realtime terminal usage, and revisioned provider jobs. Base URL routing
distinguishes OpenAI, both current Azure OpenAI host forms, OpenRouter, and
Perplexity. It preserves the requested Azure deployment without storing request
content.

Evidence includes `tests/test_openai_current_http_compat.py`,
`tests/test_openai_multimodal_instrument.py`, `tests/test_openai_realtime.py`,
`tests/test_openai_provider_jobs.py`, and
`tests/test_openai_compatible_provider_routing.py`.

### OpenRouter

Official sources:

- <https://openrouter.ai/docs/quickstart>
- <https://openrouter.ai/docs/api/reference/overview>
- <https://github.com/OpenRouterTeam/python-sdk>

Verified against `openrouter==1.1.54`. The first-class adapter covers Chat,
Responses, embeddings, images, STT, TTS, rerank, native sync/async streams,
video jobs, and generation-cost reconciliation. Provider-reported total and
upstream cost are authoritative. Routed provider, BYOK, cache, reasoning,
service tier, and tool quantities are retained only as safe attribution fields.
The OpenAI-compatible route is also recognized without misclassifying ordinary
OpenAI traffic.

Evidence: `tests/test_openrouter_current_compat.py`.

### Perplexity

Official sources:

- <https://github.com/perplexityai/perplexity-py>
- <https://docs.perplexity.ai/docs/getting-started/quickstart>
- <https://docs.perplexity.ai/docs/getting-started/pricing>
- <https://docs.perplexity.ai/guides/chat-completions-sdk>

Verified against `perplexityai==0.43.3` and the current OpenAI compatibility
path. The native adapter covers all four currently documented core billable API
families:

- Agent API Responses create/stream plus background create/retrieve/cancel;
- Sonar Chat Completions;
- Search;
- Embeddings and Contextualized Embeddings.

It records exact nested provider cost, input/output/cache-write/cache-read/
reasoning/citation/query/tool invocation quantities, background lifecycle, and
privacy-safe failure identity. Search falls back to the published per-request
rate when the response has no exact cost. Browser Sessions are intentionally
not represented as billable attribution: the current provider documentation
does not list them among the core billed APIs and publishes no billing meter.
Inventing a cost would violate the completion contract.

Evidence: `tests/test_perplexity_current_compat.py` and
`tests/test_openai_compatible_provider_routing.py`.

### fal.ai

Official sources:

- <https://github.com/fal-ai/fal>
- <https://fal.ai/docs/model-apis/model-endpoints/client>
- <https://fal.ai/docs/documentation/model-apis/inference/queue>

Verified against `fal_client==0.14.1`. DexCost covers module helpers,
`SyncClient`, `AsyncClient`, `SyncRequestHandle`, and `AsyncRequestHandle` for
run, subscribe, stream, submit, status, result/get, and cancel. Nested calls are
suppressed so subscribe and module-level bound helpers do not double count.

Image count and actual output dimensions, actual video duration, actual audio
duration, tokens, and provider-reported cost are used when returned. A generic
request count is retained when no stronger meter exists. Endpoint names may
select catalog candidates but never fabricate media duration. A 202
`CANCELLATION_REQUESTED` acknowledgement is not terminal: fal.ai documents that
in-progress work can still complete, so reconciliation waits for terminal
evidence.

Evidence: `tests/test_fal_current_compat.py`.

### Ollama

Official sources:

- <https://github.com/ollama/ollama-python>
- <https://docs.ollama.com/api>

Verified against `ollama==0.6.2`. Module singleton, `Client`, and `AsyncClient`
chat/generate streams, current and legacy embeddings, web search, and web fetch
are covered without double counting. Terminal duration/token quantities are
native evidence; local execution is not assigned a hosted provider price.

Evidence: `tests/test_ollama_current_compat.py`.

### Google Gen AI and Vertex AI

Official sources:

- <https://googleapis.github.io/python-genai/>
- <https://github.com/googleapis/python-genai>

Verified against installed `google-genai==1.73.0` and isolated current
`google-genai==2.17.0`. Models/AsyncModels content and streams, chats through
their underlying model methods, embeddings, image generation/upscale/edit/
recontext/segmentation, foreground and background Interactions, video,
batch/tuning lifecycles, and Vertex mode share the current adapter.

The deprecated `vertexai` generative modules are excluded. Google's official
migration schedule removed those modules after 2026-06-24; implementing them
now would add a dead API rather than parity. The supported replacement is
`google-genai` with Vertex configuration.

Evidence includes `tests/test_google_genai_current_compat.py`,
`tests/test_google_video_jobs.py`, and `tests/test_google_batch_tuning_jobs.py`.

### LiteLLM routed-provider identity

Official sources:

- <https://github.com/BerriAI/litellm>
- <https://docs.litellm.ai/docs/completion>

Verified against `litellm==1.81.13`. Completion/acompletion and their streams
preserve exact provider cost, current cache/reasoning/tool usage variants, and
request model identity. The Revenium Node provider mapper is fully represented:

| LiteLLM aliases | Canonical DexCost provider/model prefix |
|---|---|
| `openai` | `openai` |
| `anthropic` | `anthropic` |
| `azure`, `azure_text` | `azure_openai` / `azure/...` |
| `azure_ai` | `azure_ai` / `azure_ai/...` |
| `google_ai_studio`, `gemini`, `palm` | `google` / `gemini/...` |
| `vertex`, `vertex_ai` | `google` / `vertex_ai/...` |
| `aws_bedrock`, `bedrock_converse` | `bedrock` / `bedrock/...` |
| `cohere` | `cohere` |
| `hugging_face`, `huggingface_hub` | `huggingface` / `huggingface/...` |
| `together`, `together_ai` | `together` / `together_ai/...` |
| `ollama` | `ollama` / `ollama/...` |
| `mistral` | `mistral` / `mistral/...` |
| `groq` | `groq` / `groq/...` |
| `openrouter` | `openrouter` / `openrouter/...` |
| `perplexity`, `perplexity_ai` | `perplexity` / `perplexity/...` |
| `fal`, `fal_ai` | `fal_ai` / `fal_ai/...` |

Evidence: `tests/test_litellm_current_compat.py`.

### Existing direct providers and tools

Anthropic, Amazon Bedrock Runtime, Cohere, and MCP remain supported and are part
of the full Python regression suite. Their existing direct attribution is not
replaced by LiteLLM mapping. Provider instrumentation remains the authoritative
LLM cost path; MCP and `track_tool` remain the authoritative explicit tool path.

## Framework closure

Framework wrappers provide workflow, capability, and tool attribution while
direct provider adapters remain the usage authority.

| Framework | Python status | Boundary |
|---|---|---|
| CrewAI | Verified | Current Crew/Agent/LiteAgent/Flow sync, async, streams, native tools, failures, and context isolation. |
| Griptape | Verified | Structure run/stream and public event listener; does not replace provider drivers. |
| LangChain / LangGraph | Verified existing trace integration | Provider events remain authoritative; framework spans attach workflow identity without duplicating provider cost. |
| Custom frameworks | Verified foundation | Task, attach, capability, outcome, revenue, and `track_tool` APIs. |

Detailed evidence is in `FRAMEWORK-COMPATIBILITY.md`.

## Server-authoritative catalog contract

Catalog JSON is moving to the control plane without moving pricing evaluation
out of the SDK. The implemented release protocol distributes immutable,
content-addressed artifacts for observer rules, LLM prices, service prices,
compute, GPU, egress, and the server pricing reference.

The Python runtime:

1. fetches the stable manifest with ETag/304 support;
2. validates SDK contract bounds, expiry, size, SHA-256, schema, and semantics;
3. activates all artifacts atomically;
4. persists a validated last-known-good release;
5. keeps working offline or through a server failure from that release;
6. refuses downgrade, corruption, partial activation, and unsafe observer rules;
7. records release/artifact provenance in pricing results.

This avoids shipping a stale full catalog in every release while preserving
low-latency local metering and offline safety. The current full JSON bootstrap
must remain until Python and TypeScript both pass first-run, air-gapped,
previous-release, corruption, expiry, and joint conformance gates. Only then is
it replaced by a small emergency bootstrap—not by an empty SDK.

Control-plane implementation and operations are documented in
`control-plane/docs/catalog-release-service.md`. Python evidence is in
`tests/test_catalog_releases.py`, `tests/test_pricing_refresh.py`, service
catalog tests, and the compute/GPU/egress integrity suites.

## Release gates before TypeScript

The Python contract freezes only after all of these are green together:

1. focused real-package/wire compatibility for every provider above;
2. scanner, optional extras, auto-instrument defaults, and public exports;
3. immediate, stream, failure, cancellation, capability, idempotency, privacy,
   delayed-job, outcome, revenue, and attribution-v3 conformance;
4. catalog release/cache/offline/corruption/downgrade/expiry behavior;
5. the complete Python test suite.

TypeScript parity begins from this frozen Python contract, not directly from
Revenium and not from an earlier TypeScript implementation. Revenium is the
minimum comparison input, never the DexCost product boundary.
