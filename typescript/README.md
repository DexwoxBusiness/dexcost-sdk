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

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEXCOST_API_KEY` | API key (if not passed to `init()`) |
| `DEXCOST_ENV` | Set to `development` for dev console output |

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

## Express Middleware

```typescript
import { createExpressMiddleware } from '@dexcost/sdk';

app.use(createExpressMiddleware({
  taskType: 'api_request',
  extractCustomerId: (req) => req.headers['x-customer-id'],
}));
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
