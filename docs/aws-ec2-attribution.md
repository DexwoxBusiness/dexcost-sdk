# EC2 invoice allocation evidence

Use only for work executing on the explicitly identified local EC2 instance:

```python
from dexcost import wrap_runtime_handler

work = wrap_runtime_handler(
    work, tracker, service_key="aws_ec2",
    billing_account_id="111111111111",  # AWS payer account
    resource_id="222222222222.us-east-1.i-1234567890abcdef0",
)
```

```typescript
import { wrapRuntimeHandler } from "@dexcost/sdk";

const trackedWork = wrapRuntimeHandler(work, tracker, {
  serviceKey: "aws_ec2", billingAccountId: "111111111111",
  resourceId: "222222222222.us-east-1.i-1234567890abcdef0",
});
```

The resource format is `usage-account.region.instance-id`; payer and linked
accounts are deliberately separate. Call inside an active DexCost task. There
are no AWS API calls or credentials here. Both SDKs emit matching unknown-cost
`runtime.task_seconds` observations and full start/end intervals. The wrapper
does not claim wall time equals billed instance time, CPU usage or storage.

Opting in replaces this task's automatic local EC2 compute/GPU estimate to avoid
counting it alongside invoice allocation. Do not combine it with manual legacy
compute/GPU cost wrappers. Other provider jobs and Lambda pricing are untouched.
If the task's runtime is known to be Lambda, a container or another runtime, the
EC2 wrapper passes through without recording EC2 usage. Unknown runtime requires
the caller's explicit, correct identity mapping; don't wrap remote API waits.

Actual amounts require a final reconciled EC2 CUR 2.0 CSV via the control plane's
existing `/v1/provider-billing-reports` endpoint with `service_key="aws_ec2"`.
This is not an automatic AWS account connector. Match the emitted full resource
ID (`payer/usage-account.region.instance-id`) and use an explicit comparable
work-seconds allocation denominator for the same resource/window. Missing
denominators, unobserved/idle work and unsupported charges stay unallocated.
Only resource-specific On-Demand `RunInstances`/`BoxUsage` lines have a driver;
Savings Plans, reservations, Spot, storage, network and account charges are not
priced from the SDK. Zero/negative invoice corrections belong to the server.

Official [EC2 terms](https://aws.amazon.com/ec2/pricing/on-demand/) and
[CUR billing fields](https://docs.aws.amazon.com/cur/latest/userguide/table-dictionary-cur2-line-item.html)
were browser-verified on 2026-10-03. No public list rate is embedded in this path.
