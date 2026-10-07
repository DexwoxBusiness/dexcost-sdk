# Qdrant Cloud and Zilliz serverless evidence

Verified against official documentation and pinned native packages on 2026-10-07.
These opt-in facades capture usage, not cash prices. They do not fetch an invoice,
make hidden provider requests, store vectors/documents/filters, or estimate storage.

| Service | Python | TypeScript | Meter |
| --- | --- | --- | --- |
| Qdrant Cloud | `instrument_qdrant`, sync/async `query_points` with REST | `instrumentQdrant`, `query` / `search` | Response `usage.hardware.cpu`, an allocation weight, **not vCPU-seconds or dollars** |
| Zilliz serverless | `instrument_zilliz`, sync/async `MilvusClient.search` | `instrumentZilliz`, `MilvusClient.search` or `HttpClient.search` | Provider-reported read vCU only |

Both languages require an explicit billing account, region and cluster hostname.
Use `cloud_vector_resource_id(provider, account, region, host)` or
`cloudVectorResourceId(provider, account, region, host)` for the matching invoice
resource and resource scope ID. Provider keys are `qdrant_cloud` and
`milvus_zilliz`. The identity is `account/SHA256(JSON([provider,region,host]))`.
Bindings assert ownership; they do not authenticate an account lookup. Qdrant
regions are explicit inventory bindings; Zilliz region must also match its
`cluster.serverless.region.vectordb.zillizcloud.com` hostname.

```python
from dexcost import instrument_qdrant, instrument_zilliz

qdrant = instrument_qdrant(qdrant_client, tracker, billing_account_id="account-a",
    region="aws-us-west-2", cluster_host="cluster.us-west.cloud.qdrant.io")
zilliz = instrument_zilliz(milvus_client, tracker, billing_account_id="org-a",
    region="aws-us-west-2",
    cluster_host="in01-example.serverless.aws-us-west-2.vectordb.zillizcloud.com")
```

Use the returned facade. Cleanup aliases are `uninstrument_qdrant` /
`uninstrumentQdrant` and `uninstrument_zilliz` / `uninstrumentZilliz`.
The original native client, return value, errors and transport remain intact.
The facade owns native calls so generic HTTP capture cannot duplicate them.
Repeated awaiting of the same native promise does not emit another event.
Missing/invalid counters are not zero; reported zero creates no positive weight
and does not declare zero paid money. Only successful native replies are observed.
An operation's actual invocation-to-response interval is preserved.

Qdrant's high-level SDK drops envelope usage; a call-local generated-API observer
reads it before stripping without changing the shared client. Python gRPC,
raw/generated calls, batch/fanout, writes, and provider inference usage are outside
this slice. Zilliz dedicated/on-demand/self-hosted Milvus, writes, iterators,
session/routing overrides and raw APIs are excluded. An omitted `extra.cost`
(Python) or `status.extra_info.report_value` (Node gRPC) cannot be reconstructed
from result count. Node HTTP reads the documented response `cost` field as vCU.

The server accepts reconciled invoice slices for `qdrant_cloud/cluster_compute`
with `qdrant.hardware.cpu` denominator and `milvus_zilliz/read_vcu` with
`zilliz.read_vcu` denominator. A CPU denominator is total CPU **counter units**,
never the provider's billed vCPU-hours. Exact account/cluster/region/closed period
and `invoice_reconciled: true`, `allocation_population: "all_usage_including_free"`
are required. Include free/included, external and uninstrumented usage in totals.
Unavailable totals leave the whole invoice slice residual; observed totals larger
than the supplied complete total withdraw allocation until corrected. This is
proportional invoice attribution, not a marginal per-request price.

Pinned no-network native regression packages: `qdrant-client==1.19.1`,
`pymilvus==3.0.2`, `@qdrant/js-client-rest@1.19.0` (Node 22+ upstream),
`@zilliz/milvus2-sdk-node@3.0.6`. Generic facade checks also run on Node 20.

Sources:

- https://api.qdrant.tech/api-reference/search/query-points
- https://qdrant.tech/pricing/
- https://docs.zilliz.com/reference/restful/search-v2
- https://docs.zilliz.com/docs/serverless-cluster-cost
- https://github.com/milvus-io/pymilvus/blob/v3.0.2/pymilvus/client/search_result.py

See the control-plane `docs/cloud-vector-attribution.md` for complete daily usage
import and the explicit final-invoice reconciliation boundary.
