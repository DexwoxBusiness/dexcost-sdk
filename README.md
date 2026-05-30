<div align="center">

# DexCost SDKs

**Agent Unit Economics — track what each AI task actually costs.**

Open-source SDKs that attribute LLM calls, non-LLM service fees, and retry waste to
your customers, projects, and workflows — across **Python, TypeScript, Rust, and Go**.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/dexcost?label=pypi)](https://pypi.org/project/dexcost/)
[![npm](https://img.shields.io/npm/v/%40dexcost%2Fsdk?label=npm)](https://www.npmjs.com/package/@dexcost/sdk)
[![crates.io](https://img.shields.io/crates/v/dexcost?label=crates.io)](https://crates.io/crates/dexcost)
[![Go Reference](https://pkg.go.dev/badge/github.com/DexwoxBusiness/dexcost-go.svg)](https://pkg.go.dev/github.com/DexwoxBusiness/dexcost-go)

</div>

---

## What is DexCost?

AI agents rack up cost from many places — LLM tokens, vector DBs, scraping APIs,
payment/notification services, and the silent tax of retries. DexCost answers a simple
question that's surprisingly hard to measure: **"what did this task cost, and who was it
for?"**

You wrap a unit of work in a `task`, and the SDK automatically captures the LLM calls and
outbound service calls that happen inside it — cost, tokens, latency, model, provider —
and attributes them to a `customer`, `project`, and workflow. It works fully **locally**
(no account, data stays on your machine) and can **optionally** stream usage to the
DexCost cloud if you want dashboards and cross-service rollups.

```python
import dexcost

dexcost.init(storage="local")       # force local-only; nothing leaves your machine
dexcost.set_context(customer_id="acme-corp")

with dexcost.task(task_type="summarise_doc") as t:
    # LLM calls are auto-captured — just use OpenAI/Anthropic/etc. normally
    resp = openai.chat.completions.create(model="gpt-4o", messages=[...])
    t.record_cost(service="pdf_parser", cost_usd="0.002")   # non-LLM cost
```

---

## Why DexCost

- **Auto-instrumentation** — patches major LLM SDKs (OpenAI, Anthropic, Gemini, Bedrock,
  Cohere, LiteLLM/Vercel AI) and common HTTP clients, so cost capture needs *no code
  changes* inside your task.
- **Non-LLM costs too** — a built-in catalog of 160+ paid services (vector DBs, scraping,
  comms, payments, …) turns outbound HTTP calls into priced `external_cost` events.
- **Retry-aware** — surfaces the cost of retries and failures, not just the happy path.
- **Per-customer / per-project attribution** — every event is tagged so you can answer
  "what does customer X cost us?"
- **Local-first & private** — runs standalone with an embedded SQLite buffer; cloud push
  is opt-in.
- **Cross-language parity** — the four SDKs share behavior and are validated against the
  same [fixtures](fixtures/) so results match regardless of language.

---

## SDKs

| Language | Package | Install | Docs |
|----------|---------|---------|------|
| **Python** | [`dexcost`](https://pypi.org/project/dexcost/) (PyPI) | `pip install dexcost` | [python/README.md](python/README.md) |
| **TypeScript / Node** | [`@dexcost/sdk`](https://www.npmjs.com/package/@dexcost/sdk) (npm) | `npm install @dexcost/sdk` | [typescript/README.md](typescript/README.md) |
| **Rust** | [`dexcost`](https://crates.io/crates/dexcost) (crates.io) | `cargo add dexcost` | [rust/README.md](rust/README.md) |
| **Go** | `github.com/DexwoxBusiness/dexcost-go` | `go get github.com/DexwoxBusiness/dexcost-go` | [go/README.md](go/README.md) |

Each SDK's README has the full, language-idiomatic API, configuration, and examples.

---

## Quick start by language

<details open>
<summary><b>Python</b></summary>

```bash
pip install dexcost            # add [all] for every provider: pip install "dexcost[all]"
```
```python
import dexcost

dexcost.init()
with dexcost.task(task_type="resolve_ticket") as t:
    t.record_llm_call("openai", "gpt-4o", input_tokens=800, output_tokens=150)
    t.record_cost(service="pinecone", cost_usd="0.001")
```
→ [Full Python docs](python/README.md)
</details>

<details>
<summary><b>TypeScript / Node</b></summary>

```bash
npm install @dexcost/sdk       # LLM SDKs are peer deps: npm install @dexcost/sdk openai
```
```typescript
import { init, track, close } from '@dexcost/sdk';

init();
await track({ taskType: 'summarise', customerId: 'acme' }, async (task) => {
  const resp = await openai.chat.completions.create({ model: 'gpt-4o', messages: [...] });
  task.recordCost('pdf_parser', 0.002);
});
await close();
```
→ [Full TypeScript docs](typescript/README.md)
</details>

<details>
<summary><b>Rust</b></summary>

```bash
cargo add dexcost
```
```rust
use dexcost::{Config, TaskOptions, TaskStatus, init, start_task, close};
use rust_decimal_macros::dec;

#[tokio::main]
async fn main() {
    init(Config::default()).unwrap();
    let mut task = start_task("resolve_ticket", TaskOptions {
        customer_id: Some("acme-corp".into()), ..Default::default()
    }).await.unwrap();
    task.record_llm_call("openai", "gpt-4o", 1000, 500, None, None, None).await.unwrap();
    task.record_cost("google_maps", dec!(0.005), None, None).await.unwrap();
    task.end(TaskStatus::Success).await.unwrap();
    close();
}
```
→ [Full Rust docs](rust/README.md)
</details>

<details>
<summary><b>Go</b></summary>

```bash
go get github.com/DexwoxBusiness/dexcost-go
```
```go
import dexcost "github.com/DexwoxBusiness/dexcost-go"

dexcost.Init(dexcost.Config{Storage: "local"})
defer dexcost.Close()

ctx, task := dexcost.StartTask(context.Background(), "resolve_ticket",
    dexcost.WithCustomer("acme-corp"))
task.RecordLLMCall("openai", "gpt-4o", 1000, 500)
task.RecordCost("google_maps", decimal.NewFromFloat(0.005))
task.End(dexcost.StatusSuccess)
_ = ctx
```
→ [Full Go docs](go/README.md)
</details>

---

## Core concepts

| Concept | What it is |
|---------|-----------|
| **Task** | A unit of work (`summarise_doc`, `resolve_ticket`, …). Everything is attributed to a task. |
| **LLM call** | Token usage + cost for a model call. Auto-captured for supported providers; can be recorded manually. |
| **Non-LLM cost** | A fee from any other paid service — recorded manually or auto-priced from the service catalog. |
| **Retry** | Wasted work; tracked so failure cost is visible. |
| **Context** | `customer_id` / `project_id` attached to tasks for attribution. |

---

## Local vs. cloud

By default the SDK is **local-only** — events are buffered in an embedded SQLite database
and nothing leaves your machine. If you provide an API key (or `DEXCOST_API_KEY`), the SDK
**also** pushes usage to the DexCost cloud (`https://api.dexcost.io`) for dashboards and
rollups. The cloud is optional; the SDKs are fully functional without it.

---

## Repository layout

```
dexcost-sdk/
├── python/        Python SDK        → PyPI: dexcost
├── typescript/    TypeScript SDK    → npm: dexcost
├── rust/          Rust SDK          → crates.io: dexcost
├── go/            Go SDK            → go get …/dexcost-go
├── fixtures/      shared cross-SDK test fixtures (parity)
├── docs/          additional documentation
└── scripts/       repo tooling
```

---

## Contributing

Contributions are welcome. Please see [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). For security issues, see [SECURITY.md](SECURITY.md).
For support and contact channels, see [SUPPORT.md](SUPPORT.md).

## License

[MIT](LICENSE) © Dexwox Innovations. Each SDK is also individually MIT-licensed.
