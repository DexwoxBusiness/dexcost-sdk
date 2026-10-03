# S3 and R2 request evidence

This opt-in facade records request counters, not money or stored capacity. Pair it
with the server's reconciled S3 CUR import or R2 normalized invoice import. Do not
multiply object bytes by a monthly storage rate or use list-price requests as cash.

```python
from dexcost import instrument_object_storage

client = instrument_object_storage(
    boto_s3_client, tracker, provider="aws_s3",
    billing_account_id="111111111111", bucket_owner_account_id="222222222222",
    bucket="agent-artifacts", region="us-east-1", owner_pays=True,
)
# Within the owning DexCost task, use normal get_object/put_object/list_objects_v2.
```

```typescript
import { instrumentObjectStorage } from "@dexcost/sdk";

const client = instrumentObjectStorage(s3Client, tracker, {
  provider: "aws_s3", billingAccountId: "111111111111",
  bucketOwnerAccountId: "222222222222", bucket: "agent-artifacts",
  region: "us-east-1", ownerPays: true,
});
// Within the owning task, use normal send(GetObjectCommand/PutObjectCommand/ListObjectsV2Command).
```

Python supports synchronous boto3 and awaited compatible native clients;
TypeScript supports promise-based AWS SDK v3 `send`. The facade leaves the original
client methods intact and adds one named read-only transport observer; use
`uninstrument_object_storage` / `uninstrumentObjectStorage` to disable capture.
The shared observer is a no-op outside an active facade invocation. Request
evidence is isolated across asynchronous calls and cleared on success or error.
Callback-style send is passed through without capture.

AWS requires matching direct endpoint/region plus observed outgoing bucket route,
an explicit owner-pays mapping and
a general-purpose bucket. Standard GET/PUT and LIST are counted; Requester Pays,
other classes, custom endpoints, errors, retries and multipart/copy are excluded.

For R2 choose `provider="r2_cloudflare"`, the 32-hex Cloudflare account ID,
`region="auto"`, and the bucket; omit AWS owner fields. The native client's
endpoint must exactly match `https://<account>.r2.cloudflarestorage.com`.
The actual outgoing route must match too; a custom endpoint provider, missing
transport observation, mismatched bucket/account or multiple attempts is excluded.
GET responses and PUT arguments must explicitly provide `StorageClass` equal to
`STANDARD` or `STANDARD_IA`; missing R2 class is unknown, not Standard. Only GET
Class B and PUT Class A are captured initially. R2 LIST, Workers bindings and
jurisdiction endpoints are outside this slice. DELETE/abort/401 never create a
billable request counter.

AWS identity is `<payer>/<owner>.<region>.<bucket>`; R2 identity is `<account>/r2`,
the **whole account billing endpoint**, shared across buckets. The server must use
the same identity, exact class and billing interval. R2 invoice denominators must
be all account/class requests in unrounded units, including free and unobserved
usage; invoice amounts must already include free allowances and rounding. Missing
denominators leave money residual. Different accounts and classes never share
pools. Partial capture does not assign the entire account bill to observed tasks.

Only stable request ID, successful status, one-attempt metadata and operation
count are used. No object key, content, returned object listing, byte estimates,
credentials, headers or prices persist. Response objects and streaming bodies are
returned untouched. Replayed IDs deduplicate, and the owning task is retained
across awaits. Full request intervals survive billing boundaries.

Official pricing/field evidence browser-verified 2026-10-03:

- https://aws.amazon.com/s3/pricing/
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/aws-usage-report-understand.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/BucketBilling.html
- https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html
- https://docs.aws.amazon.com/boto3/latest/guide/retries.html
- https://developers.cloudflare.com/r2/pricing/
- https://developers.cloudflare.com/r2/buckets/storage-classes/
- https://developers.cloudflare.com/r2/api/s3/api/

The server documentation contains full monetary authority and import requirements.
These SDKs do not calculate storage byte-months, minimum-duration charges, retrieval
fees, free-tier balances or provider-wide rounding from individual requests.
