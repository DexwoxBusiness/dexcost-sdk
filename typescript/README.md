# @dexcost/sdk

**Agent Unit Economics SDK for Node.js** — track LLM costs, non-LLM service fees, and retry waste attributed to customers, projects, and workflows.

## Install

```bash
npm install @dexcost/sdk
```

With LLM provider SDKs (peer dependencies):

```bash
npm install @dexcost/sdk openai @anthropic-ai/sdk
```

### Module formats (ESM **and** CommonJS)

The package ships **both** an ES module and a CommonJS build, so it works in
either project type with no async-import workarounds:

```typescript
// ESM / TypeScript with "module": "ESNext" | "NodeNext"
import { init, track, close } from '@dexcost/sdk';
```

```javascript
// CommonJS (NestJS and most production Node.js apps compiled to CommonJS)
const { init, track, close } = require('@dexcost/sdk');
```

Because the CJS entry is synchronous, `init()` can run as a plain side-effect
import **before** any LLM SDK loads — which is required for
auto-instrumentation to patch the providers. Put it first in your entry point:

```javascript
// instrument.js — imported at the very top of main.ts/main.js
const { init } = require('@dexcost/sdk');
init({ apiKey: process.env.DEXCOST_API_KEY });
// ...then import your app, which imports openai/@anthropic-ai/sdk
```

### Native dependency: `better-sqlite3`

The local event buffer uses [`better-sqlite3`](https://github.com/WiseLibs/better-sqlite3),
a **native** module that compiles on install (needs `python3`, `make`, and a
C/C++ compiler such as `g++`). It is an **optional dependency**:

- **If it builds**, events are persisted to `~/.dexcost/buffer.db` and survive
  process restarts.
- **If it is missing or fails to compile**, the SDK does **not** crash — it
  automatically falls back to a bounded in-memory buffer (10k-entry cap, events
  do not survive a restart) and logs a one-line warning telling you how to
  restore durable buffering.

In Docker multi-stage builds, the native binding can be installed but not
compiled for the runtime stage's Node ABI (the "Could not locate the bindings
file" error). Rebuild it in the stage that runs your app:

```dockerfile
RUN npm rebuild better-sqlite3
```

Or, if you need a fully pure-JS deployment (Vercel Edge, Cloudflare Workers,
distroless images), simply omit `better-sqlite3` and rely on the in-memory
fallback.

## Quick Start

```typescript
import { init, track, close } from '@dexcost/sdk';

init({ apiKey: 'dx_live_...' });  // or set DEXCOST_API_KEY env var

await track({ taskType: 'summarise', customerId: 'acme' }, async (task) => {
  // LLM calls are auto-captured — just use OpenAI/Anthropic normally
  const response = await openai.chat.completions.create({
    model: 'gpt-4o',
    messages: [{ role: 'user', content: 'Summarise this document' }],
  });

  // Record non-LLM costs manually
  task.recordCost('pdf_parser', 0.002);
});

await close();
```

## Auto-Instrumentation

dexcost auto-instruments **6 LLM providers** and the **global fetch API**.

### LLM Providers

| Provider | Package | Auto-Patched |
|----------|---------|-------------|
| OpenAI | `openai` | `chat.completions.create` |
| Anthropic | `@anthropic-ai/sdk` | `messages.create` |
| Vercel AI | `ai` | Vercel AI SDK functions |
| Google Gemini | `@google/generative-ai` | `generateContent` |
| AWS Bedrock | `@aws-sdk/client-bedrock-runtime` | `invokeModel` |
| Cohere | `cohere-ai` | `chat` / `generate` |

LLM provider packages are **peer dependencies** — install only the ones you use. dexcost detects them at runtime and patches automatically.

> **Vercel AI SDK v5+:** the `ai` package ships ESM-only builds since v5, which
> **cannot be monkey-patched** (you will see a one-line warning at init). Calls
> are still captured at the HTTP layer, but for exact usage — multi-step tool
> loops, cached tokens — wrap your models with the middleware below.

### Vercel AI SDK middleware (recommended for `ai` >= 5)

```typescript
import { wrapLanguageModel } from 'ai';
import { anthropic } from '@ai-sdk/anthropic';
import { init, dexcostAiMiddleware } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });

const model = wrapLanguageModel({
  model: anthropic('claude-sonnet-4-5'),
  middleware: dexcostAiMiddleware(),
});
// use `model` with generateText / streamText as usual — every call is captured
```

Works with ai v3 through v7 (`LanguageModelV1Middleware` … `V4Middleware`
shapes), on generate **and** stream, under any bundler or module system.
The middleware and the other capture layers coordinate — one call never
records twice.

### Injectable fetch (any client, no global patch)

Every provider client (`openai`, `@anthropic-ai/sdk`, all `@ai-sdk/*`
factories) accepts a `fetch` option — inject a tracked one instead of
relying on the global patch:

```typescript
import { createDexcostFetch } from '@dexcost/sdk';

const anthropic = createAnthropic({ fetch: createDexcostFetch() });
const openai = new OpenAI({ fetch: createDexcostFetch() });
```

Same classification pipeline as the global patch (OpenAI/Anthropic/Gemini
formats, SSE streaming, byte counting), refuses to double-wrap when the
global patch is active, and never breaks client construction (falls back
to the base fetch, loudly, if dexcost is unwired).

### OpenTelemetry bridge (ingestion only)

If your app emits OTel spans — the Vercel AI SDK does natively via
`experimental_telemetry` — dexcost can consume them in-process instead of
intercepting anything:

```typescript
import { NodeSDK } from '@opentelemetry/sdk-node';
import { init, DexcostSpanProcessor } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });
const sdk = new NodeSDK({ spanProcessors: [new DexcostSpanProcessor()] });
sdk.start();

// per call:
await generateText({ model, prompt, experimental_telemetry: { isEnabled: true } });
```

**One-way, in only.** The processor is not an exporter: it converts LLM
spans (AI SDK telemetry + GenAI semconv attribute names) into dexcost cost
events shipped to the dexcost endpoint, and nothing else. It coexists with
any exporters you already run (Datadog etc. keep seeing exactly what they
saw), never reads prompt/completion content — only model, provider, token
counts, and timing — and a cross-layer fingerprint guard prevents double
counting when the patched fetch captures the same call.

### Bundled apps (`instrumentModules` escape hatch)

Bundlers (Next.js, webpack, esbuild) can inline provider packages so
dexcost's runtime resolution patches a DIFFERENT copy than the one your
code calls — instrumented in name, capturing nothing. Hand the instruments
your actual module references:

```typescript
import OpenAI from 'openai';
import Anthropic from '@anthropic-ai/sdk';

init({ instrumentModules: { openai: OpenAI, anthropic: Anthropic } });
```

Keys: `openai`, `anthropic`, `ai`, `gemini`, `bedrock`, `cohere`, `mcp`.
Providing a module implies instrumenting it, and its activation failures
are surfaced loudly.

### Instance wrappers

```typescript
import { wrapOpenAI, wrapAnthropic } from '@dexcost/sdk';
const openai = wrapOpenAI(new OpenAI());       // chat.completions surface
const anthropic = wrapAnthropic(new Anthropic()); // messages surface
```

### HTTP (Non-LLM Cost Capture)

dexcost patches `globalThis.fetch` to capture HTTP calls to domains in the [163-service catalog](src/data/service_prices.json) — Pinecone, Twilio, SendGrid, Stripe, Firecrawl, Exa, and more. Costs are extracted from response headers/body and recorded as `external_cost` events.

### Controlling Instrumentation

```typescript
// Instrument only specific providers
init({ autoInstrument: ['openai', 'anthropic'] });

// Disable all auto-instrumentation
init({ autoInstrument: [] });
```

## Configuration

### `init()` Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `apiKey` | `string` | `DEXCOST_API_KEY` env | API key for cloud push |
| `endpoint` | `string` | `https://api.dexcost.io` | Control Layer URL. Set explicitly in code (e.g. `http://localhost:3000` for local). Must start with `http://` or `https://`. **Never read from the environment** — see note below. |
| `autoInstrument` | `string[]` | All 6 providers | Which LLM SDKs to patch |
| `batchSize` | `number` | `100` | Events per sync batch |
| `flushIntervalMs` | `number` | `30000` | Milliseconds between sync pushes |
| `redactFields` | `string[]` | `undefined` | Field names to redact from event details |
| `hashCustomerId` | `boolean` | `false` | SHA-256 hash customer_id before storage |
| `environment` | `string` | `undefined` | Set to `"development"` for dev console mode |
| `dbPath` | `string` | `~/.dexcost/buffer.db` | Path to local SQLite buffer |
| `enableRetryHeuristics` | `boolean` | `false` | Auto-detect retries via pattern matching |
| `debug` | `boolean` | `false` | Log every capture decision to stderr (see Debugging capture) |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEXCOST_API_KEY` | API key (if not passed to `init()`) |
| `DEXCOST_ENV` | Set to `development` for dev console output |
| `DEXCOST_DEBUG` | Set to `1` to log every capture decision to stderr |

> **Note:** `DEXCOST_ENDPOINT` is no longer read. The endpoint is sourced
> **only** from the explicit `endpoint` option in code (defaulting to
> `https://api.dexcost.io`). This is a deliberate security measure: an attacker
> who controls the process environment cannot redirect telemetry or the Bearer
> API key by setting `DEXCOST_ENDPOINT=http://attacker/`.

## API

### Singleton Pattern

dexcost uses a singleton. Call `init()` once at app startup:

```typescript
import { init, getTracker, track, flush, close } from '@dexcost/sdk';

init({ apiKey: 'dx_live_...' });

// Use the global track() anywhere
await track({ taskType: 'chat', customerId: 'acme' }, async (task) => {
  task.recordLlmCall('openai', 'gpt-4o', 800, 150);
});

// Or get the tracker instance
const tracker = getTracker();
```

### TrackedTask Methods

```typescript
await track({ taskType: '...' }, async (task) => {
  // Record LLM call manually (usually auto-captured)
  task.recordLlmCall('openai', 'gpt-4o', 800, 150);

  // Record non-LLM cost
  task.recordCost('pinecone', 0.001);

  // Record usage (cost computed from registered rates)
  task.recordUsage('s3_storage', 1024);

  // Mark a retry
  task.markRetry('rate_limit', 0.005);

  // Link to external trace
  task.linkTrace('datadog', 'trace-abc123');

  // End with status (auto-detected from exceptions)
  task.end('success');
});
```

### Customer Attribution

```typescript
import { setContext, track } from '@dexcost/sdk';

setContext({ customerId: 'acme-corp', projectId: 'proj-alpha' });

// All tasks inherit the context
await track({ taskType: 'resolve_ticket' }, async (task) => {
  // task.task.customerId === 'acme-corp'
});
```

### Context Propagation

dexcost uses `AsyncLocalStorage` — task context propagates across `await`, `Promise.all`, `setTimeout`, and any async operation without manual threading.

```typescript
import { getCurrentTask } from '@dexcost/sdk';

// Inside any async function within a tracked task:
const task = getCurrentTask();  // Returns the active Task or undefined
```

### Nested Tasks

```typescript
await track({ taskType: 'pipeline' }, async (parent) => {
  await track({ taskType: 'step_1' }, async (child) => {
    // child.task.parentTaskId === parent.task.taskId (auto-linked)
  });
});
```

## Storage modes (cloud vs local)

The SDK decides whether to sync to the Dexcost Control Layer from **two**
independent signals, so it is always clear why data is or isn't being sent:

| Condition | Result |
|-----------|--------|
| Valid API key present (`apiKey` option or `DEXCOST_API_KEY`) **and** not in dev mode | **cloud** — events buffered locally and pushed in the background |
| No API key, or `storage: "local"` | **local** — events buffered locally, never pushed (no warning; this is normal) |
| `environment: "development"` / `DEXCOST_ENV=development` | **dev mode** — events printed to the console, cloud sync disabled regardless of API key |

`environment: undefined` does **not** trigger dev mode — only the literal
string `"development"` does. So passing `environment: undefined` in production
leaves cloud sync enabled (assuming a valid API key); you will **not** see the
"development mode active" message in that case.

## Dev Mode

Set `DEXCOST_ENV=development` or pass `environment: "development"` to `init()`. In dev mode:
- Cost events are printed to the console
- No data is pushed to the cloud (this is intentional and logged once at startup)

## HTTP Framework Middleware

Each request runs inside a tracked task (`req.dexcostTask` /
`c.get('dexcostTask')`), so LLM and HTTP calls made while handling it are
attributed automatically. The tracker argument is optional everywhere — it
defaults to the `init()` singleton, resolved lazily per request.

### Express / Connect

```typescript
import { init, createExpressMiddleware } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });

app.use(createExpressMiddleware({
  customerIdFrom: 'headers.x-customer-id',   // dot-path into req
  skip: (req) => req.path === '/health',
}));
```

### Fastify

```typescript
import { init, dexcostFastifyPlugin } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });

await app.register(dexcostFastifyPlugin, {
  customerIdFrom: 'headers.x-customer-id',
  skip: (req) => req.url === '/health',
});
```

### Hono (Node, Bun, Deno)

```typescript
import { init, createHonoMiddleware } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });

app.use('*', createHonoMiddleware({
  customerId: (c) => c.req.header('x-customer-id'),
  skip: (c) => c.req.path === '/health',
}));
```

### NestJS

```typescript
import { APP_INTERCEPTOR } from '@nestjs/core';
import { init, DexcostInterceptor } from '@dexcost/sdk';

init({ apiKey: process.env.DEXCOST_API_KEY });

@Module({
  providers: [
    {
      provide: APP_INTERCEPTOR,
      useValue: new DexcostInterceptor({
        customerIdFrom: 'headers.x-customer-id',
        skip: (req) => req.url === '/health',
      }),
    },
  ],
})
export class AppModule {}
```

Duck-typed (no `@nestjs/common` dependency); borrows the host app's own
`rxjs` lazily and subscribes the handler chain inside the task's async
scope, so controllers and services inherit attribution. Works on both the
Express and Fastify platforms; non-HTTP contexts (RPC/WS/GraphQL) pass
through — wrap those with `wrapJobHandler` or `track()`.

Task status comes from the response status code (>= 400 → `failed`) or a
thrown handler error. dexcost failures never block a request; handler errors
are never swallowed.

## Queue Workers

Queue consumers are where agent workloads actually run. Wrap the handler so
every job gets its own attributed task:

```typescript
import { wrapJobHandler } from '@dexcost/sdk';

new Worker('reviews', wrapJobHandler(
  async (job) => runReview(job.data),
  {
    taskType: 'code_review',
    customerId: (job) => job.data.orgId,
    metadata: (job) => ({ pr_number: job.data.prNumber }),
  },
));
```

Handler errors are marked `failed` and re-thrown, so your queue's retry
semantics are untouched. Works with any consumer signature (BullMQ,
RabbitMQ `(msg, channel)`, SQS, cron ticks).

## Serverless

Freeze-prone platforms (Lambda, Cloud Functions, Vercel, Cloud Run) give no
background CPU after the handler returns — the background pusher may never
fire. The compute wraps (`wrapLambdaHandler`, `wrapVercelHandler`, …) now
flush automatically before returning (bounded at 3s, never throws over your
handler's result). For hand-rolled handlers and Next.js routes:

```typescript
import { after } from 'next/server';
import { flushBeforeFreeze } from '@dexcost/sdk';

export async function POST(req: Request) {
  const result = await handleRequest(req);
  after(() => flushBeforeFreeze());   // flush outside the response path
  return result;
}
```


## Debugging capture: debug mode & `dexcost doctor`

When the dashboard shows less than you expect, two tools answer "why wasn't
this call captured?":

```typescript
init({ debug: true });   // or DEXCOST_DEBUG=1
```

Debug mode logs every capture decision to stderr: which instruments
activated (and why not), how each HTTP call was classified (`llm_call` via
fallback, network event, suppressed), and session lifecycle.

```bash
npx dexcost doctor            # full pipeline check
npx dexcost doctor --offline  # skip the endpoint reachability probe
```

Doctor verifies the whole chain — runtime (Node/Bun/Deno), AsyncLocalStorage,
better-sqlite3 vs memory fallback, provider packages (including the `ai` >= 5
ESM caveat), an instrument dry-run, the fetch patch, a buffer write/read
round-trip, API-key format, and endpoint reachability — with a remedy line
for everything degraded. Exit code 1 when any check fails.

## Runtimes: Node, Bun, Deno

- **Node >= 18** is the primary target.
- **Bun** gets DURABLE buffering via the built-in `bun:sqlite` driver
  (better-sqlite3's native binding is unsupported there — the SDK switches
  automatically). **Deno** runs on the in-memory buffer (no loadable SQLite
  driver). Both propagate context via Node-compat AsyncLocalStorage; use
  the Hono middleware for request tracking, and run `npx dexcost doctor`
  to see exactly what your runtime supports. All three runtimes are
  exercised in CI on every commit.
- **Edge runtimes** (Cloudflare Workers, Vercel Edge) are not supported by
  the Node SDK; the browser adapter covers client-side capture.

## Subpath imports

Heavier integrations can be imported without pulling the root barrel:

```typescript
import { createHonoMiddleware } from '@dexcost/sdk/middleware';
import { dexcostAiMiddleware } from '@dexcost/sdk/integrations/ai-sdk';
import { DexcostSpanProcessor } from '@dexcost/sdk/integrations/otel';
import { DexcostCallbackHandler } from '@dexcost/sdk/integrations/langchain';
import { TrackedOpenAI } from '@dexcost/sdk/clients';
```

## Runtime Dependencies

- `better-sqlite3` — local event buffer (**optional, native** — see
  [Native dependency](#native-dependency-better-sqlite3) above; the SDK falls
  back to an in-memory buffer when it is unavailable)
- `ajv` — JSON Schema validation
- `js-yaml` — rate file parsing

## Development

```bash
npm install
npm test
npm run build
npm run lint
```

## Releases

Releases are generated from Conventional Commit pull-request titles and are
squash-merged to `main`. Use `feat(typescript): ...` for features and
`fix(typescript): ...` for fixes. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
