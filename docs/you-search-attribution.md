# You.com base Search attribution

Verified 2026-10-08. This is opt-in capture of successful direct **base Search**
requests, paired in Python and TypeScript. Server JSON owns the published gross
rate; the SDK records usage, never dollars. `paid` is the caller's assertion that
this exact client/transport uses the public PAYG account. It does not prove cash
owed: free credits, prepaid credits, volume discounts and enterprise agreements
must not be inferred from an API key or search response.

## Python native client

Tested against official `youdotcom==3.5.0`, including sync and async requests.
The current SDK's operation defaults to `https://ydc-index.io`, independently of
the client's general `server_url`. The exact actual request must match the bound
endpoint; `https://api.you.com` is also supported when explicitly selected for
the operation. Do not put credentials in `billing_account_id`.

```python
from youdotcom import You
from dexcost import instrument_you_search, uninstrument_you_search

native = You(api_key_auth=api_key)
you = instrument_you_search(native, tracker, billing_account_id="you-account-a",
    endpoint="https://ydc-index.io", billing_tier="paid")
# Inside an existing DexCost task:
result = you.search(query="Search terms", count=10)
result = await you.search_async(query="Search terms", count=10)
uninstrument_you_search(you)  # Does not close the native client.
```

## TypeScript supported fetch route

The current official integrations documentation deprecates `@youdotcom-oss/sdk`
and recommends REST `fetch` for TypeScript. No dependency on the deprecated SDK
is introduced. Use the returned fetch function; each matching request is captured
automatically, without manually forwarding a response.

```typescript
import { createYouSearchFetch, uninstrumentYouSearch } from "@dexcost/sdk";
const searchFetch = createYouSearchFetch(tracker, {
  billingAccountId: "you-account-a", endpoint: "https://ydc-index.io",
  billingTier: "paid", fetch,
});
// Inside an existing DexCost task:
const response = await searchFetch("https://ydc-index.io/v1/search", {
  method: "POST", headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
  body: JSON.stringify({ query: "Search terms", count: 10 }),
});
const result = await response.json(); // Original Response/body preserved.
uninstrumentYouSearch(searchFetch);
```

## Exact boundary

- Only a successful HTTP 200 direct `/v1/search` with a valid provider
  `metadata.search_uuid` and structured `results` records one request, including
  empty results. Result count does not multiply the base request charge.
- A durable account-scoped hash of that UUID deduplicates repeated responses and
  keeps the first owning task. Separate successful UUIDs are separate requests.
  Python captures native retries but only successful provider responses count;
  TypeScript adds no retries. Errors, transport failures, missing IDs or redirects
  produce no priced evidence, not an assertion that the provider charged zero.
- `free` and `unknown` preserve observed base usage but omit paid eligibility.
  Unknown parameters, machine-payment headers/routes, extraction (even cache or
  highlights), legacy livecrawl options, Contents, Answer and Research are outside
  this slice. Deprecated Python `search.unified` helpers remain callable but
  uncaptured. No hidden polling or balance requests.
- Use a new binding after changing accounts, credentials or pricing eligibility.
  A billing account identifier is caller-supplied scope, not authenticated invoice
  verification. A custom fetch/HTTP transport is trusted to dispatch honestly.
- Queries, result bodies, URLs from results, header values and API keys are not
  stored. Only stable hashed identity, task ownership, times, count and pricing
  eligibility reach the ledger. Existing HTTP capture ownership prevents a second
  event for the same native call.

## Official evidence

- [Billing](https://you.com/docs/administration/billing): base Search USD5 per
  1,000 calls; live full-page extraction is an additional separately billed meter.
- [Search guide](https://you.com/docs/guides/search) and
  [API reference](https://you.com/docs/api-reference/search/v1-search): direct
  endpoint, 1–100 results, response metadata and distinct extraction options.
- [Current SDK guidance](https://you.com/docs/integrations/python-sdk): current
  Python client and deprecated TypeScript package; use fetch for JavaScript.
- [Official Python source](https://github.com/youdotcom-oss/youdotcom-python-sdk):
  native callable Search shim, asynchronous method, transport and retry semantics.
