# Tracking kodus-ai LLM costs with dexcost

kodus-ai never calls dexcost APIs itself, so this integration runs in
**ambient mode**: `init()` patches `globalThis.fetch`, LLM calls to any
OpenAI/Anthropic-compatible endpoint (including BYOK vendors like Kimi,
DeepSeek, Z.ai behind base-path prefixes) are captured at the HTTP layer,
and per-job attribution comes from `runWithContext()`.

## 1. Install in the kodus orchestrator

```bash
pnpm add @dexcost/sdk
```

pnpm's strict `node_modules` layout means the SDK must be a direct
dependency so it resolves the same provider packages as the app.

## 2. Bootstrap every process that makes LLM calls

kodus runs `api`, `worker`, and `webhooks` as separate containers; **the
worker is where reviews (and therefore LLM calls) execute**. Copy
`dexcost-bootstrap.ts` next to each app's `main.ts` and make it the FIRST
import:

```ts
// apps/worker/src/main.ts
import "./dexcost-bootstrap"; // ← before NestFactory, before everything
```

Ordering matters for kodus's legacy v2 (LangChain) engine: its
`@anthropic-ai/sdk` client captures a reference to `fetch` when the client
is constructed, so the patch must already be installed. The v5 agent
engine (Vercel AI SDK v6) resolves `fetch` per call and is order-safe.

Expected log line on startup:

```
[dexcost] Failed to instrument vercel-ai: Could not patch the 'ai' module: ...
```

This is normal — `ai` v5+ ships ESM-only exports that cannot be
monkey-patched. Those calls are captured by the HTTP fallback instead
(same events, provider = the endpoint hostname).

## 3. Attribute each review job

Wrap the RabbitMQ review-job handler in the worker with `runWithContext`
(see `review-job-context.ts`). Each job then gets its own session task
labeled with org/repo, aggregating every LLM call, retry, and byte of
network egress for that review:

```ts
await runWithContext(
  { customerId: org, projectId: repo, agent: "kodus_code_review" },
  () => runReview(job),
);
```

Without this you still capture everything, but it groups into anonymous
process-wide `agent_session` tasks with no customer attribution.

## 4. What lands on the dashboard

Per review job, one task with:

- `llm_call` events per model call — tokens, latency, token-priced cost
  (LLM dimension), plus `request_bytes`/`response_bytes` in details;
- network byte aggregates and egress dollars (`network_cost_usd`,
  Network dimension) — the same wire traffic priced at your cloud's
  egress rate, separate from token cost;
- status `success` once the job's session goes idle (30s) or the process
  shuts down cleanly.

## 5. Optional: graduate to explicit mode

Once ambient capture is proven, replace `runWithContext` with
`getTracker().track({...}, fn)` at the same boundary for exact task
start/end timing and parent/child nesting of sub-steps. See the comment
block at the bottom of `review-job-context.ts`.
