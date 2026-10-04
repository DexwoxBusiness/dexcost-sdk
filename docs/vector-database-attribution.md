# Native vector usage and reconciled invoice allocation

Verified 2026-10-04 against official API documentation and the public pricing pages
rendered in the browser. This is opt-in usage capture plus normalized invoice
allocation, **not automatic invoice retrieval or per-request cash pricing**.

To disable capture on an instrumented facade, call Python
`uninstrument_pinecone(facade)` / `uninstrument_turbopuffer(facade)`, or TypeScript
`uninstrumentPinecone(facade)` / `uninstrumentTurbopuffer(facade)`, matching the
instrumenter used. These public cleanup functions are idempotent; they do not
close or change the native provider client.

## Admitted operations

| Provider | Native calls | Provider evidence |
| --- | --- | --- |
| Pinecone | `query`, `fetch` | `usage.read_units` / `usage.readUnits`, including fractional RU |
| turbopuffer | namespace `query` | `billing.billable_logical_bytes_queried` and `..._returned` |
| turbopuffer | namespace `write` | `billing.billable_logical_bytes_written`; optional `billing.query` queried/returned bytes |

Python sync and asyncio clients are covered. The synchronous Pinecone gRPC
result shape can use the same facade; gRPC future/thread-pool calls, fanout,
integrated inference, document search, write-unit estimation and all unlisted
helpers are excluded. TypeScript supports the pinned native HTTP clients.
Known upstream boundary: Pinecone Python 10.0.0 currently declares `read_units`
as an integer and rejects a fractional JSON RU while parsing, before DexCost can
observe the reply. Its native error is preserved and no usage is invented.
Python's capture adapter and server allocation accept exact fractional RU when
provided, but **fractional native HTTP decoding is not covered by this provider
version**. The real-client regression explicitly tests that failure. TypeScript
9.0.0 decodes and captures fractional RU in the real mocked-I/O gate.
Raw/streaming helpers are not captured; TypeScript `withResponse()` captures
parsed data once, and `asResponse()` remains native and does not record usage.

Native response counters become ordinary, local-ID usage events—not provider
jobs or asserted provider invoices. Returned bytes are a network event; queried
and written bytes are storage events. The conservative measured invocation
interval is preserved, so operations straddling a billing boundary stay residual.
The native facade owns its underlying HTTP capture and suppresses duplicate HTTP
usage events. Retries may consume unreported usage: successful response counters
are partial evidence, never the complete invoice denominator.

## Bind the actual billing resource

Pass an explicit billing account, region, endpoint and namespace from your
provider inventory. These are caller-owned billing identity assertions, not
account discovery. Only endpoint/namespace metadata is inspected; no additional
provider call, key inspection, vector, filter, document or record-ID capture occurs.
Custom proxies/transports and altered authentication are not verified account
proof. Bind only a native client that actually belongs to the declared account.

```python
from dexcost import instrument_pinecone, vector_database_resource_id

tracked_index = instrument_pinecone(
    index, tracker, billing_account_id="account-a", region="us-east-1",
    index_host="memory-abc.svc.aped-4627-b74a.pinecone.io", namespace="agent-memory",
)
# Inside an active DexCost task; original arguments/results stay unchanged.
result = tracked_index.query(vector=query_vector, top_k=5, namespace="agent-memory")
invoice_resource = vector_database_resource_id(
    "pinecone", "account-a", "us-east-1",
    "memory-abc.svc.aped-4627-b74a.pinecone.io", "agent-memory",
)
```

```typescript
import { instrumentTurbopuffer, vectorDatabaseResourceId } from "@dexcost/sdk";
const tracked = instrumentTurbopuffer(client.namespace("agent-memory"), tracker, {
  billingAccountId: "account-a", region: "gcp-us-central1", namespace: "agent-memory",
});
const result = await tracked.query({ rank_by: ["vector", "ANN", queryVector], top_k: 5 });
const invoiceResource = vectorDatabaseResourceId("turbopuffer", "account-a",
  "gcp-us-central1", "gcp-us-central1.turbopuffer.com", "agent-memory");
```

Use the returned `account/SHA256(JSON([provider,region,host,canonicalNamespace]))` value
as BOTH `resource.id` and `scope.id` (`endpoint` resource, `resource` scope).
Pinecone's empty namespace and reserved `__default__` name identify the same
default namespace, so both canonicalize to `__default__` before hashing, matching
its [Python namespace semantics](https://sdk.pinecone.io/python/how-to/vectors/namespaces.html)
and [TypeScript namespace semantics](https://sdk.pinecone.io/typescript/documents/data-operations_namespaces.html).
Turbopuffer requires a nonempty namespace. Namespaces are not persisted in plain
text. Different accounts, regions, endpoints and distinct namespaces cannot
match the same allocation resource.

## Invoice admission and money boundary

Submit actual final amounts through `/v1/provider-billing-costs`:

```json
{
  "schema_version": "1", "service_key": "pinecone_query", "charge_category": "read_units",
  "provider_record_id": "invoice-2026-09-read-units", "billing_account_id": "account-a",
  "revision": 1, "invoice_reconciled": true,
  "scope": {"type": "resource", "id": "REPLACE_WITH_INVOICE_RESOURCE"},
  "resource": {"type": "endpoint", "id": "REPLACE_WITH_INVOICE_RESOURCE"},
  "billing_period": {"start_at": "2026-09-01T00:00:00Z", "end_at": "2026-10-01T00:00:00Z"},
  "currency": "USD", "amount": "8", "allocation_basis_quantity": "2",
  "effective_at": "2026-10-01T00:00:00Z", "observed_at": "2026-10-02T00:00:00Z"
}
```

The numbers are a test illustration, not provider prices: `.25 + .75` observed RU
against a **provider-reconciled total of 2 RU** allocates half of the actual $8
invoice slice and leaves $4 residual. Supply the whole matching population,
including free/included usage where it belongs; never use captured task usage as
the denominator. RU can be fractional; billable-byte denominators must be integers.
Use stable line IDs and sequential revisions for credits and corrections. Final
zero amounts remain restorable; replay does not append another charge.

The amount and denominator must cover the SAME account/resource/period/currency/
charge population. If only a whole-account bill is available, do not fabricate
namespace subtotals or copy the bill once per namespace: retain it unallocated
until reconciled subtotals exist. A known amount without a denominator stays
residual. Storage, minimum commitments and unmeasured categories stay residual.

This is proportional allocation of invoice money, not a claim of exact marginal
request prices. Different plans/discount classes must not be represented as
independently exact request prices. Server JSON profiles—not SDK constants—own
allocation admission. No public-rate multiplication occurs in these wrappers.

## Pricing evidence and exclusions

Pinecone documents response RU, fractional query RU with a 0.25 minimum, and
separate storage/plan minimums. Builder included capacity and Standard/Enterprise
commitments make response counters insufficient to determine cash paid. We do
not round RU or turn upserted vector counts into write units or stored GB-months.

turbopuffer separates queried, returned and written bytes. Reported billable
query bytes already reflect the minimum and marginal discounts. Write billable
bytes include attribute adjustments but not all batch/copy discounts. We neither
reapply floors nor price raw bytes; actual reconciled invoice amounts absorb
allowances, discounts and plan commitments. Never infer retained storage from a
write response, namespace statistics or request content.

Official sources:

- https://docs.pinecone.io/guides/manage-cost/understanding-cost
- https://sdk.pinecone.io/python/reference/grpc.html
- https://sdk.pinecone.io/typescript/classes/Index.html
- https://turbopuffer.com/pricing
- https://turbopuffer.com/docs/query
- https://turbopuffer.com/docs/write
- https://github.com/turbopuffer/turbopuffer-python
- https://github.com/turbopuffer/turbopuffer-typescript

Mock-I/O compatibility gates use Pinecone Python 10.0.0 / TypeScript 9.0.0 and
turbopuffer Python 2.10.2 / TypeScript 2.9.0. No paid provider calls or credentials
are required. Both SDKs consume `fixtures/vector_database_conformance.json`.
Pinecone TypeScript 9 requires Node22; its real-client gate skips on Node20 while
the dependency-free capture/conformance tests continue to run there.
