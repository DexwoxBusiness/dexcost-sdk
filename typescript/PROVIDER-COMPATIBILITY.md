# TypeScript provider compatibility

This matrix describes first-class instrumentation shipped by `@dexcost/sdk`.
Provider packages are optional peers: install only the SDKs used by the
application. Generic HTTP observation is a fallback, not a substitute for the
provider-specific lifecycle and usage tests below.

| Instrument | Package / protocol | Captured surfaces and identity |
|---|---|---|
| `openai` | `openai` | Chat Completions, Responses, streaming, usage APIs, and Realtime/WebSocket; request/response IDs, token/cache/audio/reasoning meters, provider-reported cost where present |
| `anthropic` | `@anthropic-ai/sdk` | Messages generate/stream plus stable and beta Message Batches; input/output, cache read/write, request identity, batch request counts, durable job revisions, and result usage after complete iteration |
| `vercel-ai` | `ai` | Generate/stream/rerank middleware and compatible patched exports; multi-step usage with cross-layer deduplication |
| `gemini` | `@google/generative-ai` | Generate/stream content; prompt/candidate/cache usage and response identity |
| `google-genai` | `@google/genai` | Models generate/stream plus live/realtime usage and lifecycle |
| `bedrock` | `@aws-sdk/client-bedrock-runtime` | Invoke/converse and streaming response usage across Bedrock model providers; embeddings, images, rerank, and durable async-invoke jobs without retaining S3/ARN identity |
| `cohere` | `cohere-ai` | V1/V2 chat, generate, embed, and rerank plus streaming usage/errors and billed-unit normalization |
| `mcp` | `@modelcontextprotocol/sdk` | Tool invocation, typed usage/cost dimensions, capability and operation identity |
| `litellm` | LiteLLM Proxy through the official `openai` client; optional injected compatible module | Chat/Responses, streams, embeddings, image/audio/OCR/rerank meters, upstream-provider and gateway-cost identity, and durable response/video/batch/fine-tuning jobs |
| `ollama` | `ollama` | Chat/generate and streaming prompt/eval counts |
| `openrouter` | `@openrouter/sdk`, underlying calls made by `@openrouter/agent`, plus OpenAI-compatible HTTP fallback | Chat, Responses, embeddings, image generation, STT, TTS, rerank, natural/failed/cancelled streams, durable video jobs, and exact generation reconciliation; disjoint token/cache/reasoning and service meters, generation/job identity, provider/upstream cost, upstream provider, BYOK, and service tier |
| `perplexity` | `@perplexity-ai/perplexity_ai` plus HTTP fallback | Chat/search usage, citations/search-context meters where returned, request identity, errors |
| `fal` | `@fal-ai/client` | Run/subscribe/queue lifecycle, usage/cost response fields, queued provider-job revisions |

## Activation

All instruments are attempted by default and missing optional packages stay
quiet. Narrow activation with `autoInstrument`, or pass the application’s
actual module reference through `instrumentModules` when a bundler has created
a second package copy.

```typescript
init({
  autoInstrument: ["openai", "openrouter", "perplexity", "fal"],
  instrumentModules: { openrouter: OpenRouter },
});
```

Vercel AI v5+ is ESM-only and cannot always be monkey-patched after module
evaluation. Prefer `dexcostAiMiddleware()` for exact multi-step/stream usage.

LiteLLM's supported production JavaScript path is its OpenAI-compatible Proxy,
not a nonexistent general-purpose `litellm` npm inference SDK. Set
`DEXCOST_LITELLM_PROXY_URL` (or `LITELLM_PROXY_URL`), construct the official
`openai` client with that base URL, and keep the `openai` instrument enabled.
DexCost then records the upstream provider separately from the `litellm`
gateway. `instrumentModules.litellm` remains available only for applications
that already expose a compatible JavaScript module surface.

`@openrouter/agent` exposes an immutable ESM standalone
`callModel(client, ...)` and an `OpenRouter` instance with an instance-owned
`callModel`. DexCost captures each standalone run’s billable calls through the
supplied `@openrouter/sdk` client, including intermediate tool-loop rounds. An
Agent `OpenRouter` instance can be instrumented directly by passing that exact
instance as `instrumentModules.openrouter`. For bundler-created platform-SDK
copies, provide the actual `OpenRouter` SDK module/class; do not pass the
immutable Agent module namespace.

## Privacy and correctness boundaries

- Prompt and completion bodies are not captured by default.
- Provider response cost wins when the provider supplies it; catalog pricing
  remains visibly provisional and carries its catalog/pricing version.
- Streaming is finalized once, including cancellation/error paths, and capture
  layers share deduplication keys so one provider call is not charged twice.
- Unknown provider chunks or terminal usage shapes degrade to safe unpriced
  request evidence; telemetry parsing never replaces a valid provider value
  with an instrumentation exception.
- Provider work that completes asynchronously is recorded as immutable
  `ProviderJobRevision` snapshots rather than fabricated synchronous events.
- Unknown future usage meters are retained as typed, visibly unpriced evidence
  instead of being silently dropped.

The complete machine-readable coverage and evidence references live in
`../contracts/python-vnext/v1/provider-capabilities.json`.
