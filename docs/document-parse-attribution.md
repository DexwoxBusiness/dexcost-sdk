# Hosted LlamaParse v2

Verified 2026-10-03 against the [official response contract](https://developers.llamaindex.ai/llamaparse/parse/guides/response-format/), [Python SDK](https://github.com/run-llama/llama-parse-py/blob/main/src/llama_cloud/resources/parsing.py), [TypeScript SDK](https://github.com/run-llama/llama-parse-ts/blob/main/src/resources/parsing.ts), and rendered [pricing](https://www.llamaindex.ai/pricing).

Wrap your current `llama-cloud` client once, with opaque billing-account and project IDs. Only hosted `parsing.create`, `parsing.get`, and `parsing.parse` are observed. Call create/parse inside the initiating DexCost task. A later get retains that task's ownership even from a different task.

```python
from dexcost import instrument_llamaparse

client = instrument_llamaparse(native_llama_cloud_client, tracker,
    billing_account_id="organization_one", project_id="project_one")
# Inside the initiating DexCost task:
result = client.parsing.parse(file_id=file_id, tier="agentic", version="latest",
    expand=["markdown", "usage"])
```

```typescript
import { instrumentLlamaParse } from "@dexcost/sdk";

const client = instrumentLlamaParse(nativeLlamaCloudClient, tracker, {
  billingAccountId: "organization_one", projectId: "project_one",
});
// Inside the initiating DexCost task:
const result = await client.parsing.parse({ file_id: fileId, tier: "agentic",
  version: "latest", expand: ["markdown", "usage"] });
```

Use the real billing account mapping and the response's exact `project_id`, not an API key, document name or URL. Unknown/mismatching project or tier is not captured. The facade never changes request options or adds polling. Usage expansion is your explicit choice. Completed jobs can temporarily return null credits: they stay pending until your subsequent normal get includes numeric `job.usage.credits`. Only allowlisted job identity, tier, provider timestamps and credits persist. No file contents, prompts, URLs, names, credentials or list-price money are stored.

An explicit zero credit snapshot is valid; it does not assert a zero-dollar bill. Repeated reads are not summed. Native capture records the first verified final snapshot only. Corrections use `record_llamaparse_job` / `recordLlamaParseJob` with an explicit next revision; these replace the prior full usage, including zero and restoration. `bind_llamaparse_job` / `bindLlamaParseJob` may bind an archived create response in its original owning task. Supply `tier` if the create response omits it. Raw helpers use provider `created_at` and `updated_at`, not import time; sub-millisecond boundaries are widened outward, never shortened. A cached job retains its original task identity rather than producing duplicate usage.

Money requires a reconciled **Parse-only consumed credit** bill and provider denominator for the same account/project/window. The exact SDK resource is `billingAccountId/projectId`; the invoice scope and resource must match. Uninstrumented use and jobs crossing billing periods stay residual. Do not allocate an entire multi-project/all-product bill as a Parse-only project charge. Unproven splits, storage, subscriptions, unused prepaid credits and bonus grants remain unallocated. The public $1.25/1,000-credit reference is not multiplied into task cash.

Not captured: v1 `llama_parse`/`llama_cloud_services`, local LiteParse, BYOC, raw/streaming wrappers, Extract/Index/Classify/Split, and failed/cancelled billing without verified counters. TypeScript APIPromise response helpers remain usable but their raw path is not instrumented. Remove wrappers using `uninstrument_llamaparse` / `uninstrumentLlamaParse`.

## Unstructured boundary

Rendered [Unstructured pricing](https://unstructured.io/pricing) lists $0.015/page after 10,000 initial free pages, with custom Business pricing. Its [legacy Partition SDK](https://docs.unstructured.io/platform-api/partition-api/sdk-python) returns elements and can merge partial split responses; those are not a verified billable-page meter. This SDK removes its stale universal per-page fallback. It does not claim complete Unstructured attribution, estimate pages from output, or price local open-source parsing as hosted API usage. A verified hosted usage/export contract remains required.
