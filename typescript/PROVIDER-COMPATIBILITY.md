# TypeScript provider compatibility

This matrix describes first-class instrumentation shipped by `@dexcost/sdk`.
Provider packages are optional peers: install only the SDKs used by the
application. Generic HTTP observation is a fallback, not a substitute for the
provider-specific lifecycle and usage tests below.

| Instrument | Package / protocol | Captured surfaces and identity |
|---|---|---|
| `openai` | `openai` | Chat Completions, Responses, streaming, usage APIs, and Realtime/WebSocket; request/response IDs, token/cache/audio/reasoning meters, provider-reported cost where present |
| `anthropic` | `@anthropic-ai/sdk` | Messages generate/stream; input/output, cache read/write, request identity, errors |
| `vercel-ai` | `ai` | Generate/stream middleware and compatible patched exports; multi-step usage with cross-layer deduplication |
| `gemini` | `@google/generative-ai` | Generate/stream content; prompt/candidate/cache usage and response identity |
| `google-genai` | `@google/genai` | Models generate/stream plus live/realtime usage and lifecycle |
| `bedrock` | `@aws-sdk/client-bedrock-runtime` | Invoke/converse and streaming response usage across Bedrock model providers |
| `cohere` | `cohere-ai` | Chat/generate and streaming usage/errors |
| `mcp` | `@modelcontextprotocol/sdk` | Tool invocation, typed usage/cost dimensions, capability and operation identity |
| `litellm` | `litellm` | Completion/chat and streaming usage for the Node package surface |
| `ollama` | `ollama` | Chat/generate and streaming prompt/eval counts |
| `openrouter` | `@openrouter/sdk` plus OpenAI-compatible HTTP fallback | First-class OpenRouter generation/request identity, prompt/completion/cache/reasoning usage, provider-reported cost and upstream cost |
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

## Privacy and correctness boundaries

- Prompt and completion bodies are not captured by default.
- Provider response cost wins when the provider supplies it; catalog pricing
  remains visibly provisional and carries its catalog/pricing version.
- Streaming is finalized once, including cancellation/error paths, and capture
  layers share deduplication keys so one provider call is not charged twice.
- Provider work that completes asynchronously is recorded as immutable
  `ProviderJobRevision` snapshots rather than fabricated synchronous events.
- Unknown future usage meters are retained as typed, visibly unpriced evidence
  instead of being silently dropped.

The complete machine-readable coverage and evidence references live in
`../contracts/python-vnext/v1/provider-capabilities.json`.
