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

Task status comes from the response status code (>= 400 → `failed`) or a
thrown handler error. dexcost failures never block a request; handler errors
are never swallowed.

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
- **Bun** and **Deno** work through their Node-compat layers (`node:`
  imports, AsyncLocalStorage). `better-sqlite3` may not build there — the
  SDK falls back to the in-memory buffer automatically. Use the Hono
  middleware for request tracking, and run `npx dexcost doctor` to see
  exactly what your runtime supports.
- **Edge runtimes** (Cloudflare Workers, Vercel Edge) are not supported by
  the Node SDK; the browser adapter covers client-side capture.

## Subpath imports

Heavier integrations can be imported without pulling the root barrel:

```typescript
import { createHonoMiddleware } from '@dexcost/sdk/middleware';
import { dexcostAiMiddleware } from '@dexcost/sdk/integrations/ai-sdk';
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

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
