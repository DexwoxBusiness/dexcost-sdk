# Hosted Firecrawl and Apify usage capture

Verified against official pricing and native response documentation on
2026-10-03, including browser review. Money remains server-owned.

```python
from dexcost import instrument_firecrawl, instrument_apify

# Existing native clients; no new dependencies or requests are introduced.
firecrawl = instrument_firecrawl(firecrawl, tracker,
    billing_account_id="team_id", resource_id="key_mapping_id")
apify = instrument_apify(apify, tracker, billing_account_id="apify_user_id")
# Invoke ordinary .scrape / .actor(id).call within a DexCost task.
```

```typescript
import { instrumentFirecrawl, instrumentApify } from "@dexcost/sdk";
const web = instrumentFirecrawl(firecrawl, tracker, {
  billingAccountId: "team_id", resourceId: "key_mapping_id",
});
const actors = instrumentApify(apify, tracker, { billingAccountId: "apify_user_id" });
// Use web.scrape(...) and actors.actor(id).call(...) inside an ordinary task.
```

Use the returned facade, not the original client. Corresponding
`uninstrument_firecrawl` / `uninstrumentFirecrawl` and Apify functions disable
capture without mutating the original. Native results, receivers and errors
are preserved; telemetry failures do not fail provider work. No content,
queries, URLs, credentials or native USD estimates are persisted.

Firecrawl scrape capture requires explicit native metadata `scrape_id` and
`credits_used` (TS `scrapeId`, `creditsUsed`). Missing fields are unknown.
Current Python `SearchData` strips ID/credits, so search is **manual raw-response
capture**, not fully automatic. `record_firecrawl_search` / `recordFirecrawlSearch`
requires the original successful v2 API envelope with ID and integral credits.
Manual search also requires `occurred_at` / `occurredAt` (original request start)
and `observed_at` / `observedAt` (when its response was observed), both timezone-
aware at millisecond precision. Preserve these timestamps on archived replays;
do not substitute the later import time. Unknown request timing remains unknown.
Native scrape/search facades capture both times around the real provider call.
A request crossing billing windows stays residual in both windows; it is not
collapsed into a zero-duration request at its start.
For async crawl/batch, bind using `bind_firecrawl_job` / `bindFirecrawlJob` in the
owning task with original provider `createdAt`, then record terminal status,
`completedAt`, and exact `creditsUsed` via `record_firecrawl_job` /
`recordFirecrawlJob`. Native `CrawlJob` alone lacks the required timestamps.
No polling is added. Repeated terminal responses replay revision 2; corrections
must explicitly supply consecutive revisions 3, 4, etc. Zero clears usage and a
later nonzero revision can restore it. Never infer credit totals from pages.

Firecrawl credits are NOT dollars. The server's single `firecrawl_scrape` invoice
profile covers scrape/search/crawl under provider `firecrawl`, service `web`;
do not duplicate the invoice by endpoint. Reconciled consumed-credit money,
provider-wide quantity and exact account/resource/period are prerequisites.
Free/unused/prepaid balances and base subscriptions must not masquerade as task
charges. Cross-period async jobs remain unallocated without interval evidence.

Apify wraps `actor` / `task` start/call and `run` get/wait (`wait_for_finish`
Python, `waitForFinish` TS), validating native `userId` against the explicit
account. Native SDK polling remains its own behavior; DexCost adds none.
Initial bind is in the initiating task; later polling only completes an already
bound run and cannot capture ownership from the polling task. Separate-process
flows use `bind_apify_run` / `bindApifyRun`, then `record_apify_run` /
`recordApifyRun` against shared durable storage. Rebinding ownership is rejected.

Apify `usageTotalUsd` is ignored in SDK capture. Configure authenticated Apify
run-cost import on the server: it performs a fresh read at least sixty seconds
after terminal time, then reconciles by run ID. Re-import start ranges for late
completion/corrections. This is run service cost, not cash settlement; it does
not add subscription fees, post-run dataset/storage operations or prepaid
payments. Missing/zero/preliminary cost are different states.

Sources: [Firecrawl billing](https://docs.firecrawl.dev/billing),
[Firecrawl pricing](https://www.firecrawl.dev/pricing),
[Python response models](https://github.com/firecrawl/firecrawl/blob/main/apps/python-sdk/firecrawl/v2/types.py),
[Firecrawl status](https://docs.firecrawl.dev/api-reference/endpoint/crawl-get),
[Apify pricing](https://apify.com/pricing),
[Apify run finality](https://docs.apify.com/api/v2/actor-run-get).
