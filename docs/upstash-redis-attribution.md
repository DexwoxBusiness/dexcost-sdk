# Upstash Redis: command evidence, invoice allocation

This opt-in facade supports **single-region pay-as-you-go Redis** only. It records
completed native GET, SET, MGET, DEL, EXISTS and INCR operations inside an active
DexCost task. One MGET or DEL is one command, not one command per key. Missing keys
and zero-valued results still represent an acknowledged command, not zero money.

Python uses `upstash-redis==1.8.0`; TypeScript native compatibility is tested with
`@upstash/redis@1.39.0`. SDKs do not contain an Upstash money formula.

```python
from upstash_redis import Redis  # upstash_redis.asyncio.Redis also works
from dexcost import instrument_upstash_redis, uninstrument_upstash_redis

client = Redis(url=redis_url, token=redis_token, rest_retries=0)
tracked = instrument_upstash_redis(
    client, tracker,
    billing_account_id="account-a", database_id="database-a",
    region="us-east-1", endpoint_host="agent-memory.upstash.io",
    billing_plan="pay_as_you_go", topology="single_region",
)
# Within an existing DexCost task:
value = tracked.get("memory-key")
uninstrument_upstash_redis(tracked)  # does not close the provider client
```

```typescript
import { Redis } from "@upstash/redis";
import { instrumentUpstashRedis, uninstrumentUpstashRedis } from "@dexcost/sdk";

const client = new Redis({
  url: redisUrl, token: redisToken,
  retry: { retries: 0 }, enableAutoPipelining: false,
});
const tracked = instrumentUpstashRedis(client, tracker, {
  billingAccountId: "account-a", databaseId: "database-a",
  region: "us-east-1", endpointHost: "agent-memory.upstash.io",
  billingPlan: "pay_as_you_go", topology: "single_region",
});
// Within an existing DexCost task:
const value = await tracked.get("memory-key");
uninstrumentUpstashRedis(tracked);
```

Use the returned facade. Raw client calls, pipelines, transactions, automatic
batching, scripts, other commands, custom requesters and ambiguous routes are
not admitted. Operational commands such as PING are delegated without capture.
The TypeScript SDK's `retry: false` is **not** sufficient: the tested provider
implementation still permits a second transport attempt. The explicit zero
above is required; DexCost never changes the client's retry behavior. Python
requires redirects disabled (the native default). TypeScript custom agent,
backend or abort-signal configurations are outside this slice; the native SDK
can manufacture a response for an aborted request, which is not usage evidence.
TypeScript observes the existing global fetch without changing its options or
reading bodies. Capture requires exactly one request with HTTP 200, an actual
response URL matching the bound endpoint and `redirected: false`. Missing or
changed route evidence remains unpriced. The observer is reference-counted and
removed when its last facade is disabled; unrelated fetch calls are delegated.

Binding identity and plan/topology are explicit caller attestations, not verified
account discovery. Rebind or stop capture when database, region, topology or plan
changes. Free, Fixed, Enterprise, unknown and global/read-region-replicated plans
are excluded. A supported native result is usage evidence, never an invoice or
provider-final dollar amount. Failed/retried or uncaptured commands may still be
billed: they must remain in the complete invoice denominator and residual.

The server meter is `upstash_redis.payg_single_region_commands`, unit `Commands`,
component `storage`, provider `upstash_redis`, service `redis`. Resource identity:

```
account_id + "/" + SHA256(JSON(["upstash_redis", region, database_id,
                               endpoint_host, "payg_single_region"]))
```

Use the exported `upstash_redis_resource_id` / `upstashRedisResourceId` helper to
construct the exact matching invoice resource. Region is separately required.
No Redis keys, values, command arguments, results, tokens or exception text are
persisted. Native errors/results remain unchanged; telemetry failures do not
break the provider call. Same-operation native/HTTP capture is deduplicated.

## Invoice-to-task boundary

Submit exact reconciled invoice command lines through the existing
`POST /v1/provider-billing-costs`, service `upstash_redis`, category
`payg_single_region_commands`. Supply a stable provider invoice-line ID, its
line-local revision, closed service period, currency, exact decimal amount,
`invoice_reconciled: true`, explicit account/region/resource and the matching
`allocation_basis_quantity` in **individual billable commands**, not 100K units,
HTTP requests, keys, bytes or only captured successes. Include all provider-billed
usage in that same command population: uncaptured, failed or retried operations
where billed, plus any usage with zero incremental price. Attest
`allocation_population: "all_usage_including_free"` only after reconciliation.

The imported amount is after applicable invoice discounts/credits; do not apply
the public tariff again. A command subtotal and matching full population are
required. A database bill mixing storage/bandwidth/subscriptions must not be
labeled entirely as commands. Absent a trustworthy denominator, omit it and
preserve the source amount unallocated. Partial capture receives only its share;
contradictory over-capture withdraws allocation rather than overspending.

Storage, bandwidth, subscriptions, general adjustments and unclassified charges
have no task driver. Corrections retain their stable line identity; final zero
snapshots can later be restored. No automatic billing connector is claimed.
The Developer Stats API describes current-month counters/cost, not a closed
invoice or a verified export-completeness contract.

## Official evidence

Checked 2026-10-07T19:01:21Z (2026-10-08 local):

- [Pricing](https://upstash.com/pricing/redis): PAYG commands differ from Fixed
  plans; operational commands are excluded. Global replication and multi-zone
  high availability have different billing implications. These are not inferred.
- [REST API](https://upstash.com/docs/redis/features/restapi): command request and
  response envelopes, including multiple per-command results in a pipeline.
- [Python features](https://upstash.com/docs/redis/sdks/py/features) and
  [TypeScript auto-pipelining](https://upstash.com/docs/redis/sdks/ts/pipelining/auto-pipeline):
  retries and batching require explicit handling, not HTTP-count pricing.
- [Database stats](https://upstash.com/docs/devops/developer-api/redis/get_database_stats):
  current-month totals are not accepted as automatically reconciled invoice cash.
