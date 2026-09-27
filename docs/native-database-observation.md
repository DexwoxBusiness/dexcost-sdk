# MongoDB Atlas and Redis Cloud

Opt-in native capture for Python and TypeScript. No Go/Rust changes. These
adapters capture **usage, not prices**. No query, document, key, value, connection
string or exception message is stored. Calls need an active DexCost task; anonymous
background traffic is not turned into orphan tasks.

## Cloud resource mapping

Explicitly supply `billing_account_id` / `billingAccountId` and `resource_id` /
`resourceId`. Use the cloud billing account ID and a stable database/cluster ID
(or an operator-maintained unique project+cluster identifier when the export only
has a cluster name). Each accepts 1–100 ASCII letters, digits, `.`, `_`, `-`.
Both SDKs emit resource type `endpoint`, ID `account/resource`. Use that **same
account-qualified identity** in the control-plane invoice mapping. Do not infer
Redis Cloud or Atlas billing from a generic Redis/MongoDB hostname: these drivers
also connect to self-hosted databases.

## Python

Install `dexcost[databases]` (PyMongo 4.13+ before 5, redis-py 5/6).

```python
from dexcost import CostTracker, mongodb_command_listener, instrument_redis_client
from pymongo import MongoClient  # AsyncMongoClient works with the same listener
import redis

tracker = CostTracker()
listener = mongodb_command_listener(
    tracker, billing_account_id="atlas-org-id", resource_id="atlas-cluster-id"
)
mongo = MongoClient(MONGO_URI, event_listeners=[listener])
cache = redis.Redis.from_url(REDIS_URL)  # redis.asyncio.Redis is also supported
undo_redis = instrument_redis_client(
    cache, tracker, billing_account_id="redis-account-id", resource_id="redis-database-id"
)
# Normal driver calls inside a tracker task now produce observations.
# Shutdown instrumentation without closing the application-owned clients:
undo_redis()
listener.close()
```

The MongoDB factory reuses an active listener for the same tracker/resource to
avoid duplicate collectors. Attach it to each relevant client. Closing the shared
listener stops all its captures; create another after closing if needed.

## TypeScript

```typescript
import { CostTracker, instrumentMongoClient, instrumentRedisClient } from "@dexcost/sdk";
import { MongoClient } from "mongodb";
import { createClient } from "redis";

const tracker = new CostTracker();
const mongo = new MongoClient(MONGO_URI, { monitorCommands: true });
const stopMongo = instrumentMongoClient(mongo, tracker, {
  billingAccountId: "atlas-org-id", resourceId: "atlas-cluster-id",
});
const redis = instrumentRedisClient(createClient({ url: REDIS_URL }), tracker, {
  billingAccountId: "redis-account-id", resourceId: "redis-database-id",
});
await redis.client.connect();
// Use redis.client (the returned proxy), not the original client.
// Normal calls inside tracker.track(...) are now observed.
redis.close();
stopMongo();
```

Verified with the official MongoDB 6.x and node-redis 5.x APIs. Driver packages
remain optional. Duplicate instrumentation on the same Node client is rejected.

## Meter meaning and limits

| Capture | Meter | Meaning |
| --- | --- | --- |
| MongoDB | `mongodb_atlas.commands` / `Commands` | One wire command attempt, including retries and cursor getMore. Bulk writes count commands, not documents. |
| Redis | `redis_cloud.commands` / `Commands` | One logical command, or the queued command count when a pipeline executes. Driver-internal retries are not extra observations. |

Both emit provider name equal to the service key, provider service `database`,
component `storage`, unknown cost, and fixed operation categories read/write/other/
batch. MongoDB aggregate is `other` because an aggregation can write. MongoDB
writeErrors/writeConcernError and Redis partial pipeline errors mark the operation
failed without storing response content. Failure does not prove that no write ran.

Python captures `execute_command`, queued pipeline execution and immediate WATCH
commands. Node captures `sendCommand`, common string/hash/list/set/sorted-set
methods in the adapter's explicit allowlist, `ft.search`, `ft.aggregate`, JSON
get/set, and `multi().exec()` / `execAsPipeline()`. For an unlisted typed Node
command use `sendCommand`; it is categorized `other`. Unlisted typed calls are
not captured. Use only captured commands if choosing a command-based allocation
denominator. Both count mget/mset as one command, not one per key.

Not claimed: ioredis, Redis Cluster/PubSub, Motor, automatic cloud account discovery,
unobserved server work, storage bytes, CPU/RBU measurement, or invoice equality
from telemetry. MongoDB in-flight tracking is bounded to 1,024 commands; overflow
drops oldest observations rather than growing process memory without bound.

## Money boundary

The control plane accepts final Atlas FOCUS `BilledCost` imports with explicit
line/resource/category mappings. The default Redis FOCUS preview uses ListCost,
**not actual money**. Redis BilledCost requires a final report and an explicit
`invoice_reconciled: true` attestation after matching it to the actual invoice;
manually reconciled invoice lines can also use provider-billing-costs. Allocating database
capacity by observed command count is opt-in, approximate **allocation of exact
source money**, not a marginal query price. Non-observed work and all non-allocatable
categories remain residual. Never multiply these command counts by public prices.

Official capture APIs: [PyMongo monitoring](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/monitoring-and-logging/monitoring/),
[node-redis](https://redis.io/docs/latest/develop/clients/nodejs/),
[redis-py pipelines](https://redis.readthedocs.io/en/stable/advanced_features.html).
